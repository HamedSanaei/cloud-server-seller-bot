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
- provider cost is never modified by the markup; the selling price keeps the
  exact provider cost currency (no inference, no conversion).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cloud_platform.modules.catalog.domain import CatalogSyncLock
from cloud_platform.modules.offers.domain import (
    PRICING_MODE_MARKUP,
    CatalogSyncReport,
    CatalogSyncStateRepository,
    OfferCatalogSyncSource,
    PricingPolicy,
    SellableOfferRepository,
    TechnicalSpec,
    markup_unit_price,
)

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._sources = list(sources)
        self._offers = offers
        self._state = state
        self._lock = lock
        self._policies = dict(pricing_policies or {})

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
        if report.ok and not report.persistence_failures:
            if self._policy_for(provider_key, report.billing_model) is None:
                warnings.append("no automatic pricing policy configured; costs refreshed only")
            # The pricing/publication phase must never abort the run: a
            # failure here used to skip the state write below entirely, so a
            # sync that repaired catalog data still showed "never" in the
            # doctor. Record the failure visibly and always persist state.
            try:
                prices_updated = await self._auto_price(provider_key, report, warnings)
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
        if report.billing_model == BILLING_MODEL_HOURLY:
            state_key = f"{provider_key}.{report.billing_model}"
        else:
            state_key = provider_key
        outcome = ProviderAutoSyncReport(
            provider_key=state_key,
            ok=report.ok and not report.persistence_failures,
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
                prices_updated=prices_updated,
                published=published,
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

    async def _auto_price(
        self,
        provider_key: str,
        report: CatalogSyncReport,
        warnings: list[str],
    ) -> int:
        """Refresh selling prices of verified auto-priced rows (same currency)."""
        updated = 0
        for product_id, location_id in sorted(report.verified):
            row = await self._offers.get_by_ref(provider_key, product_id, location_id)
            if row is None or not row.provider_available:
                continue
            if row.billing_model != report.billing_model:
                warnings.append(f"{row.ref}: billing model changed; left untouched")
                continue
            policy = self._policy_for(provider_key, row.billing_model)
            if policy is None:
                continue
            if row.operator_disabled:
                # Explicit operator block: automation leaves the row alone
                # entirely (no pricing, no publishing) — silently, since a
                # standing block is intended state, not a problem to warn on.
                continue
            if not row.auto_priced:
                continue
            if row.provider_cost_minor <= 0:
                warnings.append(f"{row.ref}: no usable provider cost; left unpriced")
                continue
            currency = _valid_currency(row.provider_cost_currency)
            if currency is None:
                warnings.append(f"{row.ref}: provider cost currency missing; fail closed")
                continue
            if not row.product_id or not row.location_id or not row.name:
                warnings.append(f"{row.ref}: missing required product identity; skipped")
                continue
            if TechnicalSpec.from_metadata(row.technical_metadata).deprecated:
                warnings.append(f"{row.ref}: deprecated plan; not auto-priced")
                continue
            try:
                price = markup_unit_price(row.provider_cost_minor, policy.markup_percent)
            except ValueError as exc:
                warnings.append(f"{row.ref}: {exc}")
                continue
            if (row.selling_price_minor, row.selling_currency) != (price, currency):
                await self._offers.set_selling_price(row.id, price, currency)
                updated += 1
        return updated

    async def _auto_publish(
        self,
        provider_key: str,
        report: CatalogSyncReport,
        warnings: list[str],
    ) -> int:
        """Put verified, eligible rows on sale (never over an operator block)."""
        published = 0
        for product_id, location_id in sorted(report.verified):
            row = await self._offers.get_by_ref(provider_key, product_id, location_id)
            if row is None:
                continue
            if row.billing_model != report.billing_model:
                continue
            policy = self._policy_for(provider_key, row.billing_model)
            if policy is None or not policy.auto_publish:
                continue
            if not row.provider_available or row.selling_price_minor <= 0:
                continue
            if row.operator_disabled:
                continue
            if TechnicalSpec.from_metadata(row.technical_metadata).deprecated:
                warnings.append(f"{row.ref}: deprecated plan; not published")
                continue
            if not row.enabled:
                await self._offers.set_enabled(row.id, True)
                published += 1
        return published
