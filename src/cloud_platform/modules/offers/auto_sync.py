"""Automatic sellable-catalog refresh: sync, price, publish (STOREFRONT-V2).

The coordinator drives one provider-neutral pass over every configured
provider sync source (:class:`OfferCatalogSyncSource`, adapter-implemented):

1. **sync** — each source refreshes its offers (and location metadata) from
    the official provider APIs. A provider failure is isolated: it is
    recorded and never prevents another provider's run.
2. **price** — every observation that was successfully AND durably persisted
    this run (the report's ``verified`` set) is repriced with the
    server-owned markup policy while the row is auto-priced. Manual prices,
    missing costs/currencies/identity and deprecated plans are never touched.
3. **publish** — verified, provider-available, priced rows go on sale unless
    the operator explicitly blocked them (``operator_disabled``) or the
    policy disables auto-publication.

Safety properties:

- pricing/publication touch ONLY verified rows — never stale reads, never
  guesses;
- persistence failures suppress pricing/publication for that provider;
- the whole run holds the catalog sync lock, so two replicas (or an
  overlapping manual run) serialize instead of interleaving writes;
- provider cost is never modified by the markup; domestic offers keep their
  native selling currency and foreign offers use the configured catalog
  currency after exact reference-rate conversion.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from cloud_platform.modules.catalog.domain import CatalogSyncLock
from cloud_platform.modules.fx.domain import FxUnavailableError
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.offers.domain import (
    PRICING_MODE_MARKUP,
    CatalogSyncReport,
    CatalogSyncStateRepository,
    OfferCatalogSyncSource,
    PricingPolicy,
    SellableOffer,
    SellableOfferRepository,
    TechnicalSpec,
    has_valid_pricing_provenance,
    required_selling_currency,
    requires_currency_normalization,
)
from cloud_platform.modules.offers.pricing import CatalogOfferPricer, ReferenceRateResolver

logger = logging.getLogger(__name__)


def _verified_offer_keys(
    report: CatalogSyncReport,
) -> tuple[tuple[str | None, str, str], ...]:
    """Return account-qualified verification keys in deterministic order."""
    if report.verified_accounts:
        return tuple(sorted(report.verified_accounts))
    if report.account_aware:
        # An empty qualified set is a real failure, not permission to widen a
        # multi-account observation back to a provider-wide product/location.
        return ()
    return tuple(
        (None, product_id, location_id) for product_id, location_id in sorted(report.verified)
    )


@dataclass(frozen=True, slots=True)
class ProviderAutoSyncReport:
    """What one coordinator run did for one provider."""

    provider_key: str
    ok: bool
    discovered: int = 0
    persisted: int = 0
    prices_updated: int = 0
    published: int = 0
    retired: int = 0
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutoSyncRunReport:
    """Outcome of one coordinator run across all providers."""

    ran: bool
    providers: tuple[ProviderAutoSyncReport, ...] = ()
    reason: str | None = None

    @property
    def skipped(self) -> bool:
        return not self.ran


def pricing_policies_from_settings(settings: Any) -> dict[str, PricingPolicy]:
    """Validated automatic pricing policies from server-owned configuration.

    Unknown modes, non-integer/negative markups and non-mapping entries fail
    closed (that provider gets no automatic pricing) instead of mispricing
    the storefront. Absent entries mean "sync costs only".
    """
    raw = getattr(settings, "storefront_pricing", None) or {}
    if not isinstance(raw, Mapping):
        return {}
    policies: dict[str, PricingPolicy] = {}
    for provider_key, entry in raw.items():
        if not isinstance(entry, Mapping):
            logger.warning(
                "catalog auto-sync: ignoring invalid pricing section for %r", provider_key
            )
            continue
        mode = str(entry.get("mode", PRICING_MODE_MARKUP))
        if mode != PRICING_MODE_MARKUP:
            logger.warning(
                "catalog auto-sync: unsupported pricing mode %r for %r",
                mode,
                provider_key,
            )
            continue
        markup = entry.get("markup_percent")
        if isinstance(markup, bool) or not isinstance(markup, int) or markup < 0:
            logger.warning(
                "catalog auto-sync: invalid markup_percent %r for %r",
                markup,
                provider_key,
            )
            continue
        policies[str(provider_key)] = PricingPolicy(
            mode=mode,
            markup_percent=markup,
            auto_publish=bool(entry.get("auto_publish", True)),
        )
    return policies


def _exact_provider_rate(row: SellableOffer) -> str | None:
    params = row.billing_parameters or {}
    key = "provider_hourly_rate" if row.billing_model == "hourly" else "provider_monthly_rate"
    value = params.get(key)
    if isinstance(value, (bool, float)) or value is None:
        return None
    text = str(value).strip()
    return text or None


def _valid_currency(value: object) -> str | None:
    """3-letter uppercase ISO code or None (never inferred, never converted)."""
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        return None
    return code


class CatalogAutoSyncCoordinator:
    """Periodic provider-neutral offer refresh (sync, price, publish)."""

    def __init__(
        self,
        *,
        sources: list[OfferCatalogSyncSource],
        offers: SellableOfferRepository,
        state: CatalogSyncStateRepository,
        lock: CatalogSyncLock,
        pricing_policies: Mapping[str, PricingPolicy] | None = None,
        reference_rates: ReferenceRateResolver | None = None,
        catalog_currency: str = "USD",
        identity_ttl_seconds: int = 3600,
    ) -> None:
        self._sources = list(sources)
        self._offers = offers
        self._state = state
        self._lock = lock
        self._policies = dict(pricing_policies or {})
        self._reference_rates = reference_rates
        self._catalog_currency = str(catalog_currency or "USD").strip().upper()
        self._identity_ttl_seconds = identity_ttl_seconds

    async def run(self) -> AutoSyncRunReport:
        """Execute one refresh pass, or skip it when the lock is held."""
        async with self._lock.guard() as acquired:
            if not acquired:
                logger.info("catalog auto-sync skipped: lock held by another sync")
                return AutoSyncRunReport(
                    ran=False, reason="catalog sync lock is held by another sync"
                )
            reports: list[ProviderAutoSyncReport] = []
            for source in self._sources:
                reports.append(await self._run_provider(source))
            return AutoSyncRunReport(ran=True, providers=tuple(reports))

    async def _run_provider(self, source: OfferCatalogSyncSource) -> ProviderAutoSyncReport:
        from cloud_platform.modules.offers.domain import BILLING_MODEL_HOURLY

        provider_key = source.provider_key
        try:
            report = await source.sync_catalog()
        except Exception as exc:  # pragma: no cover - sources report, not raise
            logger.warning("catalog auto-sync for %s failed: %s", provider_key, exc)
            report = CatalogSyncReport(
                provider_key=provider_key,
                ok=False,
                complete=False,
                errors=(f"{type(exc).__name__}: {exc}",),
            )
        warnings = list(report.warnings)
        errors = list(report.errors)
        prices_updated = 0
        published = 0
        pricing_errors: list[str] = []
        if report.ok and not report.persistence_failures:
            if self._policy_for(provider_key, report.billing_model) is None:
                warnings.append("no automatic pricing policy configured; costs refreshed only")
            # The pricing/publication phase must never abort the run: a
            # failure here used to skip the state write below entirely, so a
            # sync that repaired catalog data still showed "never" in the
            # doctor. Record the failure visibly and always persist state.
            try:
                prices_updated = await self._auto_price(
                    provider_key, report, warnings, pricing_errors
                )
                published = await self._auto_publish(provider_key, report, warnings)
            except Exception as exc:
                message = f"pricing/publication failed: {type(exc).__name__}: {exc}"
                logger.warning("catalog auto-sync for %s: %s", provider_key, message)
                warnings.append(message)
                errors.append(message)
        elif not report.ok:
            logger.warning(
                "catalog auto-sync for %s: no pricing/publication (sync not usable)",
                provider_key,
            )
        else:  # pragma: no cover - defensive: persistence failures with ok=True
            logger.warning(
                "catalog auto-sync for %s: no pricing/publication (%d persistence failures)",
                provider_key,
                len(report.persistence_failures),
            )
            errors.extend(report.persistence_failures)
        # Monthly and hourly product lines share one provider key
        # ("leaseweb") but need SEPARATE sync status: the doctor must show
        # "leaseweb monthly" and "leaseweb.hourly" independently instead of
        # one row overwriting the other. Offers keep provider_key
        # "leaseweb" (pricing/publication look them up by it); only the
        # durable status row is billing-suffixed.
        if pricing_errors:
            errors.extend(pricing_errors)
        if report.billing_model == BILLING_MODEL_HOURLY:
            state_key = f"{provider_key}.{report.billing_model}"
        else:
            state_key = provider_key
        outcome = ProviderAutoSyncReport(
            provider_key=state_key,
            ok=report.ok and not report.persistence_failures and not pricing_errors,
            discovered=report.discovered,
            persisted=report.persisted,
            prices_updated=prices_updated,
            published=published,
            retired=report.retired,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )
        try:
            await self._state.record_run(
                provider_key=state_key,
                ok=outcome.ok,
                discovered=outcome.discovered,
                persisted=outcome.persisted,
                prices_updated=outcome.prices_updated,
                published=outcome.published,
                retired=outcome.retired,
                warnings=outcome.warnings,
                errors=outcome.errors,
            )
        except Exception as exc:
            # Status persistence must never fail the catalog work itself.
            logger.warning("catalog auto-sync state write failed for %s: %s", state_key, exc)
        logger.info(
            "catalog auto-sync %s: ok=%s discovered=%d persisted=%d prices=%d "
            "published=%d retired=%d warnings=%d errors=%d",
            state_key,
            outcome.ok,
            outcome.discovered,
            outcome.persisted,
            prices_updated,
            published,
            outcome.retired,
            len(outcome.warnings),
            len(outcome.errors),
        )
        return outcome

    def _policy_for(self, provider_key: str, billing_model: str) -> PricingPolicy | None:
        # Family-specific policy first (``leaseweb.hourly``), then the
        # provider-wide one (``leaseweb``); absent means costs-only.
        return self._policies.get(f"{provider_key}.{billing_model}") or self._policies.get(
            provider_key
        )

    @staticmethod
    def _pricing_failure_metadata(row: SellableOffer, reason: str) -> dict[str, object]:
        metadata = dict(row.pricing_metadata or {})
        metadata.update(
            {
                "fx_repricing_pending": True,
                "fx_last_error": reason,
                "fx_last_error_at": datetime.now(UTC).isoformat(),
            }
        )
        return metadata

    async def _auto_price(
        self,
        provider_key: str,
        report: CatalogSyncReport,
        warnings: list[str],
        pricing_errors: list[str] | None = None,
    ) -> int:
        """Reprice every verified auto-owned row from current cost and FX.

        Foreign native costs are resolved once per distinct currency pair (the
        shared resolver owns TTL/singleflight). Native provider cost columns are
        never written. Manual prices and operator-disabled rows are untouched.
        """
        updated = 0
        eligible: list[tuple[SellableOffer, PricingPolicy]] = []
        currencies: set[str] = set()
        for account_id, product_id, location_id in _verified_offer_keys(report):
            row = await self._offers.get_by_ref(
                provider_key, product_id, location_id, provider_account_id=account_id
            )
            if row is None or not row.provider_available:
                continue
            if row.billing_model != report.billing_model:
                warnings.append(f"{row.ref}: billing model changed; left untouched")
                continue
            policy = self._policy_for(provider_key, row.billing_model)
            if policy is None or row.operator_disabled:
                continue
            if not row.auto_priced:
                if requires_currency_normalization(row, self._catalog_currency):
                    warnings.append(
                        f"{row.ref}: manual {row.selling_currency} price must be "
                        f"{required_selling_currency(row, self._catalog_currency)}; "
                        "not auto-overwritten"
                    )
                continue
            if not row.product_id or not row.location_id or not row.name:
                warnings.append(f"{row.ref}: missing required product identity; skipped")
                continue
            if TechnicalSpec.from_metadata(row.technical_metadata).deprecated:
                warnings.append(f"{row.ref}: deprecated plan; not auto-priced")
                continue
            currency = _valid_currency(row.provider_cost_currency)
            if currency is None:
                warnings.append(f"{row.ref}: provider cost currency missing; fail closed")
                continue
            eligible.append((row, policy))
            if currency not in ("IRT", "IRR", self._catalog_currency):
                currencies.add(currency)

        # Warm/fetch each distinct pair once. A later per-row resolver call hits
        # the shared fresh cache, so external calls scale with currencies rather
        # than offers. Identity USD requires no source and never enters this set.
        rate_errors: dict[str, str] = {}
        prefetched_rates: dict[tuple[str, str], ReferenceRateResolution] = {}
        if currencies and self._reference_rates is None:
            for currency in sorted(currencies):
                rate_errors[currency] = "FX unavailable"
        else:
            for currency in sorted(currencies):
                try:
                    assert self._reference_rates is not None
                    get_rate = getattr(self._reference_rates, "get_rate", None)
                    if callable(get_rate):
                        resolution = await get_rate(
                            currency,
                            self._catalog_currency,
                            allow_catalog_stale=True,
                        )
                    else:
                        get_catalog_rate = getattr(self._reference_rates, "get_catalog_rate", None)
                        if not callable(get_catalog_rate):
                            raise FxUnavailableError("global catalog FX resolver is unavailable")
                        resolution = await get_catalog_rate(currency, self._catalog_currency)
                    prefetched_rates[(currency, self._catalog_currency)] = resolution
                except Exception as exc:
                    rate_errors[currency] = type(exc).__name__

        for row, policy in eligible:
            currency = _valid_currency(row.provider_cost_currency)
            if currency is None:
                continue
            if currency in rate_errors:
                reason = f"FX unavailable for {currency}->{self._catalog_currency}"
                await self._record_fx_failure(row, reason, warnings, pricing_errors)
                continue
            # All rows, including domestic IRT/IRR rows, use the same exact
            # Decimal pricer.  A rounded provider minor value is a display
            # projection, never the input to a markup decision.
            try:
                row_target = currency if currency in {"IRT", "IRR"} else self._catalog_currency
                row_pricer = CatalogOfferPricer(
                    self._reference_rates if row_target != currency else None,
                    row_target,
                    prefetched_rates=prefetched_rates,
                    identity_ttl_seconds=self._identity_ttl_seconds,
                )
                priced = await row_pricer.price_auto(row, policy)
                priced.pricing_metadata.update(
                    {
                        "provider_cost_minor": row.provider_cost_minor,
                        "provider_cost_currency": currency,
                    }
                )
            except Exception as exc:
                reason = (
                    f"FX pricing failed for {currency}->{self._catalog_currency}: "
                    f"{type(exc).__name__}"
                )
                await self._record_fx_failure(row, reason, warnings, pricing_errors)
                continue
            result = await self._offers.set_auto_price_if_current(
                row.id,
                expected_cost_minor=row.provider_cost_minor,
                expected_cost_currency=currency,
                selling_price_minor=priced.selling_price_minor,
                selling_currency=priced.selling_currency,
                pricing_metadata=priced.pricing_metadata,
                expected_provider_rate=_exact_provider_rate(row),
            )
            if result is None:
                warnings.append(f"{row.ref}: manual/operator change won pricing race")
                continue
            updated += 1
        return updated

    @staticmethod
    def _same_cost_snapshot(row: SellableOffer) -> bool:
        """Prove the previous price belongs to the exact current provider rate."""
        metadata = row.pricing_metadata or {}
        if metadata.get("provider_cost_minor") != row.provider_cost_minor:
            return False
        if (
            str(metadata.get("provider_cost_currency") or "").strip().upper()
            != (row.provider_cost_currency or "").strip().upper()
        ):
            return False
        key = "provider_hourly_rate" if row.billing_model == "hourly" else "provider_monthly_rate"
        parameters = row.billing_parameters or {}
        current = parameters.get(key)
        recorded = metadata.get(key)
        if current is None or recorded is None or isinstance(current, (bool, float)):
            return False
        try:
            if Decimal(str(current).strip()) != Decimal(str(recorded).strip()):
                return False
            source_amount = Decimal(str(metadata.get("source_amount")))
        except (InvalidOperation, TypeError, ValueError):
            return False
        return source_amount == Decimal(str(current).strip())

    async def _record_fx_failure(
        self,
        row: SellableOffer,
        reason: str,
        warnings: list[str],
        pricing_errors: list[str] | None = None,
    ) -> None:
        """Keep a valid canonical price, or leave a new/invalid offer unpriced."""
        metadata = self._pricing_failure_metadata(row, reason)
        # Preserve only a price whose metadata proves the same native-cost
        # snapshot. New/legacy rows are cleared but remain retained for a
        # deterministic later reprice; they are never deleted.
        preserve = (
            row.selling_price_minor > 0
            and row.selling_currency.strip().upper()
            == required_selling_currency(row, self._catalog_currency)
            and self._same_cost_snapshot(row)
            and has_valid_pricing_provenance(
                row,
                required_selling_currency(row, self._catalog_currency),
                catalog_stale_limit_seconds=getattr(
                    self._reference_rates, "catalog_stale_limit", None
                ),
            )
        )
        result = await self._offers.record_auto_pricing_failure_if_current(
            row.id,
            expected_cost_minor=row.provider_cost_minor,
            expected_cost_currency=row.provider_cost_currency,
            expected_price_minor=row.selling_price_minor,
            expected_selling_currency=row.selling_currency,
            expected_pricing_metadata=dict(row.pricing_metadata or {}),
            pricing_metadata=metadata,
            preserve_valid_price=preserve,
            expected_provider_rate=_exact_provider_rate(row),
        )
        if result is None:
            warnings.append(f"{row.ref}: manual/operator change won FX-failure race")
            return
        if pricing_errors is not None:
            pricing_errors.append(f"{row.ref}: {reason}")
        if preserve:
            warnings.append(
                f"{row.ref}: {reason}; preserved previous valid "
                f"{self._catalog_currency} selling price"
            )
        else:
            warnings.append(f"{row.ref}: {reason}; left unpriced and not sellable")

    async def _auto_publish(
        self,
        provider_key: str,
        report: CatalogSyncReport,
        warnings: list[str],
    ) -> int:
        """Put verified, eligible rows on sale (never over an operator block)."""
        published = 0
        for account_id, product_id, location_id in _verified_offer_keys(report):
            row = await self._offers.get_by_ref(
                provider_key, product_id, location_id, provider_account_id=account_id
            )
            if row is None:
                continue
            if row.billing_model != report.billing_model:
                continue
            policy = self._policy_for(provider_key, row.billing_model)
            if policy is None or not policy.auto_publish:
                continue
            selling_currency = required_selling_currency(row, self._catalog_currency)
            if (
                not row.provider_available
                or row.selling_price_minor <= 0
                or row.selling_currency.strip().upper() != selling_currency
                or not has_valid_pricing_provenance(
                    row,
                    selling_currency,
                    catalog_stale_limit_seconds=getattr(
                        self._reference_rates, "catalog_stale_limit", None
                    ),
                )
            ):
                if requires_currency_normalization(row, self._catalog_currency):
                    warnings.append(
                        f"{row.ref}: selling currency {row.selling_currency} violates "
                        f"catalog target {self._catalog_currency}; not published"
                    )
                continue
            if row.operator_disabled:
                continue
            if TechnicalSpec.from_metadata(row.technical_metadata).deprecated:
                warnings.append(f"{row.ref}: deprecated plan; not published")
                continue
            if not row.enabled:
                result = await self._offers.publish_if_current(
                    row.id,
                    expected_price_minor=row.selling_price_minor,
                    expected_currency=row.selling_currency,
                    expected_cost_minor=row.provider_cost_minor,
                    expected_cost_currency=row.provider_cost_currency,
                    expected_provider_rate=_exact_provider_rate(row),
                )
                if result is None:
                    warnings.append(f"{row.ref}: operator disable/price race won; not published")
                else:
                    published += 1
        return published
