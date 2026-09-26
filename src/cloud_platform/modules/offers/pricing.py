"""Provider-neutral catalog pricing with exact reference-rate arithmetic.

The provider-native cost snapshot is immutable input. This service converts an
exact Decimal major-unit cost to the configured catalog currency, applies the
operator markup, and only then rounds the customer amount upward to the target
currency's audited minor unit.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Protocol

from cloud_platform.modules.fx.domain import (
    FxPurpose,
    FxReferenceQuote,
    FxUnavailableError,
    currency_exponent,
    major_to_minor,
    minor_to_major,
    normalize_currency,
)
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    PricingPolicy,
    SellableOffer,
)


class ReferenceRateResolver(Protocol):
    """Provider-neutral exact-rate port used by catalog pricing."""

    async def get_rate(
        self,
        base_currency: str,
        quote_currency: str,
        *,
        allow_catalog_stale: bool = False,
    ) -> ReferenceRateResolution: ...

    async def get_catalog_rate(
        self,
        base_currency: str,
        quote_currency: str,
        *,
        min_remaining_lifetime_seconds: int = 0,
    ) -> ReferenceRateResolution: ...


class OfferPricingError(ValueError):
    """The provider observation cannot be priced without guessing."""


def _accepts_catalog_horizon(candidate: object) -> bool:
    """Whether a resolver callable implements the catalog publication horizon.

    Legacy/diagnostic resolvers (and scripted doubles) keep their previous
    contract; they simply cannot honour the horizon and are used as-is.
    """
    try:
        parameters = inspect.signature(candidate).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - permissive callables
        return True
    if "min_remaining_lifetime_seconds" in parameters:
        return True
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())


async def resolve_catalog_rate(
    rates: ReferenceRateResolver | None,
    base_currency: str,
    quote_currency: str,
    *,
    min_remaining_lifetime_seconds: int,
) -> ReferenceRateResolution:
    """Acquire one catalog reference rate with an explicit publication horizon.

    Catalog publication is a different concern from transactional settlement:
    the canonical path asks for a rate whose validity outlives the next
    scheduled catalog refresh (``min_remaining_lifetime_seconds``), so a freshly
    written price can never expire before the refresh that replaces it. The
    bounded last-known-good policy stays available, but it is selected by the
    resolver and always reported through ``stale``.
    """
    if rates is None:
        raise FxUnavailableError(f"FX unavailable for {base_currency}->{quote_currency}")
    get_catalog_rate = getattr(rates, "get_catalog_rate", None)
    if callable(get_catalog_rate):
        resolution: ReferenceRateResolution
        if _accepts_catalog_horizon(get_catalog_rate):
            resolution = await get_catalog_rate(
                base_currency,
                quote_currency,
                min_remaining_lifetime_seconds=min_remaining_lifetime_seconds,
            )
        else:
            resolution = await get_catalog_rate(base_currency, quote_currency)
        return resolution
    return await rates.get_rate(base_currency, quote_currency, allow_catalog_stale=True)


@dataclass(frozen=True, slots=True)
class PricedOffer:
    selling_price_minor: int
    selling_currency: str
    pricing_metadata: dict[str, object]


class CatalogOfferPricer:
    """Price native provider costs in one server-owned catalog currency."""

    def __init__(
        self,
        reference_rates: ReferenceRateResolver | None,
        target_currency: str,
        *,
        prefetched_rates: Mapping[tuple[str, str], ReferenceRateResolution] | None = None,
        identity_ttl_seconds: int = 3600,
        min_catalog_fresh_lifetime_seconds: int = 0,
        catalog_horizon_anchor: datetime | None = None,
    ) -> None:
        if (
            isinstance(identity_ttl_seconds, bool)
            or not isinstance(identity_ttl_seconds, int)
            or identity_ttl_seconds <= 0
        ):
            raise ValueError("identity_ttl_seconds must be a positive integer")
        if (
            isinstance(min_catalog_fresh_lifetime_seconds, bool)
            or not isinstance(min_catalog_fresh_lifetime_seconds, int)
            or min_catalog_fresh_lifetime_seconds < 0
        ):
            raise ValueError("min_catalog_fresh_lifetime_seconds must be a non-negative integer")
        self._rates = reference_rates
        self._prefetched_rates = dict(prefetched_rates or {})
        self._min_catalog_fresh_lifetime_seconds = min_catalog_fresh_lifetime_seconds
        # The horizon is measured from ONE anchor per catalog run, not from the
        # write instant: a provider walk that takes minutes must not shorten the
        # validity of the first rows it writes. ``None`` anchors at construction
        # time, so a directly-constructed pricer still enforces a real horizon.
        self._catalog_horizon_anchor = catalog_horizon_anchor or datetime.now(UTC)
        if self._catalog_horizon_anchor.tzinfo is None:
            raise ValueError("catalog_horizon_anchor must be timezone-aware")
        # Identity conversions carry no rate risk, but their validity is still a
        # catalog publication fact: it must cover the same horizon.
        self._identity_ttl_seconds = max(identity_ttl_seconds, min_catalog_fresh_lifetime_seconds)
        self._target = normalize_currency(target_currency)
        # Fail at composition time if the configured target has no audited
        # minor-unit exponent; unknown currencies are never assumed to be cents.
        currency_exponent(self._target)

    @property
    def target_currency(self) -> str:
        return self._target

    @property
    def min_catalog_fresh_lifetime_seconds(self) -> int:
        return self._min_catalog_fresh_lifetime_seconds

    async def price_auto(self, offer: SellableOffer, policy: PricingPolicy) -> PricedOffer:
        """Native exact cost -> target FX -> markup -> final customer rounding."""
        if policy.mode != "markup" or policy.markup_percent < 0:
            raise OfferPricingError("automatic pricing requires a non-negative markup")
        source = normalize_currency(offer.provider_cost_currency)
        source_amount, exact_source = self._auto_source_amount(offer, source)
        expected_minor = major_to_minor(source_amount, source, FxPurpose.DISPLAY)
        if expected_minor != offer.provider_cost_minor:
            raise OfferPricingError(
                "exact provider rate does not match the provider_cost_minor observation"
            )
        resolution = await self._resolution(source, self._target)
        with localcontext() as context:
            context.prec = max(
                64,
                len(source_amount.as_tuple().digits)
                + len(resolution.rate.as_tuple().digits)
                + len(str(policy.markup_percent))
                + 24,
            )
            converted = source_amount * resolution.rate
            markup = Decimal(100 + policy.markup_percent) / Decimal(100)
            selling_major = converted * markup
            selling_minor = major_to_minor(
                selling_major,
                self._target,
                FxPurpose.CHARGE,
            )
        if selling_minor <= 0:
            raise OfferPricingError("computed selling price must be positive")

        metadata = self._metadata(
            source_currency=source,
            source_amount=source_amount,
            source_basis=(
                "exact_provider_rate_major"
                if exact_source is not None
                else "provider_cost_minor_to_major"
            ),
            resolution=resolution,
            markup_percent=policy.markup_percent,
            converted=converted,
            selling_minor=selling_minor,
            pricing_mode="auto",
            provider_cost_minor=offer.provider_cost_minor,
            provider_cost_currency=source,
        )
        if exact_source is not None:
            metadata[
                "provider_hourly_rate"
                if offer.billing_model == BILLING_MODEL_HOURLY
                else "provider_monthly_rate"
            ] = exact_source
        return PricedOffer(
            selling_price_minor=selling_minor,
            selling_currency=self._target,
            pricing_metadata=metadata,
        )

    async def price_manual(self, offer: SellableOffer) -> PricedOffer:
        """Normalize an existing manual selling amount without adding markup."""
        source = normalize_currency(offer.selling_currency)
        if offer.selling_price_minor <= 0:
            raise OfferPricingError("manual selling price must be positive")
        source_amount = minor_to_major(offer.selling_price_minor, source)
        resolution = await self._resolution(source, self._target)
        with localcontext() as context:
            context.prec = max(
                64,
                len(source_amount.as_tuple().digits) + len(resolution.rate.as_tuple().digits) + 24,
            )
            converted_manual = source_amount * resolution.rate
            selling_minor = major_to_minor(
                converted_manual,
                self._target,
                FxPurpose.CHARGE,
            )
        if selling_minor <= 0:
            raise OfferPricingError("normalized manual selling price must be positive")
        metadata = self._metadata(
            source_currency=source,
            source_amount=source_amount,
            source_basis="manual_selling_minor_to_major",
            resolution=resolution,
            markup_percent=0,
            converted=converted_manual,
            selling_minor=selling_minor,
            pricing_mode="manual",
            provider_cost_minor=offer.provider_cost_minor,
            provider_cost_currency=normalize_currency(offer.provider_cost_currency),
        )
        metadata["original_selling_price_minor"] = offer.selling_price_minor
        metadata["original_selling_currency"] = source
        return PricedOffer(
            selling_price_minor=selling_minor,
            selling_currency=self._target,
            pricing_metadata=metadata,
        )

    async def _resolution(
        self, source_currency: str, target_currency: str
    ) -> ReferenceRateResolution:
        if source_currency == target_currency:
            now = datetime.now(UTC)
            return ReferenceRateResolution(
                FxReferenceQuote(
                    base_currency=source_currency,
                    quote_currency=target_currency,
                    rate=Decimal(1),
                    source="identity",
                    source_market=f"{source_currency}/{target_currency}",
                    provider_date=now.date(),
                    observed_at=now,
                    expires_at=now + timedelta(seconds=self._identity_ttl_seconds),
                ),
                stale=False,
            )
        prefetched = self._prefetched_rates.get((source_currency, target_currency))
        if prefetched is not None:
            resolution = prefetched
        else:
            if self._rates is None:
                raise FxUnavailableError(f"FX unavailable for {source_currency}->{target_currency}")
            resolution = await resolve_catalog_rate(
                self._rates,
                source_currency,
                target_currency,
                min_remaining_lifetime_seconds=self._min_catalog_fresh_lifetime_seconds,
            )
        if (
            resolution.base_currency != source_currency
            or resolution.quote_currency != target_currency
            or not isinstance(resolution.rate, Decimal)
            or not resolution.rate.is_finite()
            or resolution.rate <= 0
        ):
            raise OfferPricingError("FX resolver returned a mismatched or invalid currency pair")
        expected_source = "identity" if source_currency == target_currency else "frankfurter"
        if resolution.source != expected_source:
            raise OfferPricingError("FX resolver returned a disallowed source family")
        if resolution.quote.source_market.strip().upper() != f"{source_currency}/{target_currency}":
            raise OfferPricingError("FX resolver returned mismatched source provenance")
        return resolution

    @staticmethod
    def _auto_source_amount(
        offer: SellableOffer, source_currency: str
    ) -> tuple[Decimal, str | None]:
        if offer.billing_model == BILLING_MODEL_HOURLY:
            raw = offer.billing_parameters.get("provider_hourly_rate")
            if raw is None:
                raise OfferPricingError(
                    "hourly pricing requires exact provider_hourly_rate; "
                    "refusing rounded minor cost"
                )
            if isinstance(raw, bool) or isinstance(raw, float):
                raise OfferPricingError("provider_hourly_rate must be Decimal text")
            try:
                exact = Decimal(str(raw).strip())
            except (InvalidOperation, ValueError, AttributeError) as exc:
                raise OfferPricingError("provider_hourly_rate is not valid Decimal text") from exc
            if not exact.is_finite() or exact <= 0:
                raise OfferPricingError("provider_hourly_rate must be positive and finite")
            return exact, str(raw).strip()

        raw_monthly = offer.billing_parameters.get("provider_monthly_rate")
        if raw_monthly is not None:
            if isinstance(raw_monthly, bool) or isinstance(raw_monthly, float):
                raise OfferPricingError("provider_monthly_rate must be Decimal text")
            try:
                exact = Decimal(str(raw_monthly).strip())
            except (InvalidOperation, ValueError, AttributeError) as exc:
                raise OfferPricingError("provider_monthly_rate is not valid Decimal text") from exc
            if not exact.is_finite() or exact <= 0:
                raise OfferPricingError("provider_monthly_rate must be positive and finite")
            return exact, str(raw_monthly).strip()

        raise OfferPricingError(
            "monthly pricing requires exact provider_monthly_rate; refusing rounded minor cost"
        )

    def _metadata(
        self,
        *,
        source_currency: str,
        source_amount: Decimal,
        source_basis: str,
        resolution: ReferenceRateResolution,
        markup_percent: int,
        converted: Decimal,
        selling_minor: int,
        pricing_mode: str,
        provider_cost_minor: int,
        provider_cost_currency: str,
    ) -> dict[str, object]:
        target = resolution.quote_currency
        metadata: dict[str, object] = {
            "pricing_schema_version": 1,
            "price_source": pricing_mode,
            "pricing_mode": pricing_mode,
            "source_currency": source_currency,
            "source_amount": str(source_amount),
            "source_amount_basis": source_basis,
            "target_currency": target,
            "provider_cost_minor": provider_cost_minor,
            "provider_cost_currency": provider_cost_currency,
            "fx_rate": str(resolution.rate),
            "fx_provider": resolution.source,
            "fx_source_market": resolution.quote.source_market,
            "fx_provider_date": resolution.quote.provider_date.isoformat(),
            "fx_observed_at": resolution.observed_at.isoformat(),
            "fx_expires_at": resolution.quote.expires_at.isoformat(),
            "fx_purpose": "charge",
            "fx_stale": resolution.stale,
            "markup_percent": str(markup_percent),
            "rounding": "ROUND_CEILING",
            "converted_cost_target_exact": str(converted),
            "final_selling_price_minor": selling_minor,
        }
        if target == "USD":
            metadata["converted_cost_usd_exact"] = str(converted)
        # Every customer-facing conversion has a bounded observation window,
        # including a fresh quote. Accepted orders/instances do not consume
        # this field; they remain governed by their immutable snapshots.
        if resolution.stale:
            stale_limit = getattr(self._rates, "catalog_stale_limit", None)
            if not isinstance(stale_limit, int) or stale_limit <= 0:
                raise OfferPricingError("stale catalog FX has no bounded validity limit")
            metadata["fx_stale_limit_seconds"] = stale_limit
            valid_until = resolution.observed_at + timedelta(seconds=stale_limit)
        else:
            valid_until = resolution.quote.expires_at
        # A published catalog price must stay provable until the next scheduled
        # refresh has completed. Without this gate a "fresh" quote with minutes
        # of TTL left is written for every offer of the run and the whole market
        # silently disappears the moment that quote expires, even though the
        # provider reads were perfectly healthy (2026-09-26 Leaseweb blackout).
        # The check is measured from ONE anchor per run, and it is deliberately
        # applied to both the fresh and the bounded stale path: a refresh that
        # cannot be proven is refused here, never written as a shorter-lived one.
        if self._min_catalog_fresh_lifetime_seconds > 0:
            required_until = self._catalog_horizon_anchor + timedelta(
                seconds=self._min_catalog_fresh_lifetime_seconds
            )
            if valid_until < required_until:
                raise OfferPricingError(
                    "catalog FX provenance cannot cover the next scheduled catalog refresh"
                )
            metadata["catalog_fresh_lifetime_required_seconds"] = (
                self._min_catalog_fresh_lifetime_seconds
            )
        metadata["catalog_valid_until"] = valid_until.isoformat()
        return metadata
