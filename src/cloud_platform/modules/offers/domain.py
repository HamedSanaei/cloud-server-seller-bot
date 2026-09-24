"""Sellable catalog offers domain (LEASEWEB-MVP).

A **sellable offer** is one provider product at one location with TWO
explicit money snapshots: the provider cost (from catalog sync, integer
minor units plus exact Decimal rate) and the customer selling price. The
automatic policy may derive a foreign selling price from that cost through
exact FX and operator markup; manual prices remain an independent operator
fact. No float ever touches either snapshot.

The row is the single gate for selling:

1. ``provider_available`` — the provider currently reports the product
   (refreshed by catalog sync);
2. ``enabled`` — the operator explicitly switched it on (automatic
   publishing may do this only while ``operator_disabled`` is false);
3. ``selling_price_minor > 0`` — an explicit customer price exists
   (owned by the automatic pricing policy while ``auto_priced`` is true,
   otherwise by the operator).

Foreign customer prices must additionally use the configured canonical catalog
currency and valid exact-FX provenance. All gates must hold for the customer
to see and buy the offer.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.fx.domain import (
    GLOBAL_FIAT_CURRENCIES,
    SUPPORTED_CURRENCIES,
    FxPurpose,
    major_to_minor,
    minor_to_major,
)

#: Billing model of every offer in this module (fixed prepaid monthly).
BILLING_MODEL_PREPAID_MONTHLY = "prepaid_monthly_fixed"

#: Commercial billing models a sellable offer can carry. Monthly VPS
#: products are ordered through the ordering API and prepaid per month;
#: hourly cloud products are created through the instance API and billed by
#: time accrual. Checkout branches on this value, never on a provider name.
BILLING_MODEL_MONTHLY = "prepaid_monthly_fixed"
BILLING_MODEL_HOURLY = "hourly"
VALID_BILLING_MODELS = frozenset({BILLING_MODEL_MONTHLY, BILLING_MODEL_HOURLY})

#: Currencies owned by the domestic/payment FX family. Every other audited
#: provider-cost currency is normalized to the configured catalog currency.
DOMESTIC_PROVIDER_COST_CURRENCIES = frozenset({"IRT", "IRR"})

#: Display-only monthly equivalent of an hourly rate (730h), for estimates
#: next to the authoritative hourly price. Never used for charging.
HOURLY_MONTHLY_ESTIMATE_HOURS = 730

#: Monthly period used for renewal estimates when the provider gives no date.
ESTIMATED_MONTH_DAYS = 30


class OfferError(Exception):
    """Base error for sellable-offer operations."""


class OfferNotFoundError(OfferError):
    """The offer id does not exist."""


class OfferNotSellableError(OfferError):
    """The offer exists but is not currently sellable (disabled, unpriced or
    provider-unavailable)."""


class OfferNotEnabledError(OfferNotSellableError):
    """The offer is not enabled for sale."""


@dataclass(frozen=True, slots=True)
class TechnicalSpec:
    """Provider-neutral customer-visible technical facts about one plan.

    Normalized by the provider adapters from their own read-only APIs; the
    storefront only consumes these values and never branches on a provider.
    Every fact is optional: ``None`` means the provider catalog did not state
    it, and the UI must render a neutral "not provided" value — never guess
    True or False. Only ``deprecated`` defaults to False (absence of a
    deprecation flag is not a deprecation).
    """

    architecture: str | None = None
    cpu_type: str | None = None
    storage_type: str | None = None
    bandwidth: str | None = None
    ipv4: bool | None = None
    ipv6: bool | None = None
    backup: bool | None = None
    deprecated: bool = False

    def to_metadata(self) -> dict[str, object]:
        """JSONB-safe mapping: only stated facts (``deprecated`` always)."""
        data: dict[str, object] = {"deprecated": self.deprecated}
        for name in (
            "architecture",
            "cpu_type",
            "storage_type",
            "bandwidth",
            "ipv4",
            "ipv6",
            "backup",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        return data

    @classmethod
    def from_metadata(cls, data: dict[str, object] | None) -> TechnicalSpec:
        """Rebuild from a stored mapping; unknown keys are ignored."""
        raw = dict(data or {})
        kwargs: dict[str, object] = {}
        for name in (
            "architecture",
            "cpu_type",
            "storage_type",
            "bandwidth",
            "ipv4",
            "ipv6",
            "backup",
        ):
            if raw.get(name) is not None:
                kwargs[name] = raw[name]
        deprecated = raw.get("deprecated")
        return cls(deprecated=bool(deprecated), **kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class SellableOffer:
    """One sellable monthly offer with its two money snapshots."""

    id: UUID
    provider_key: str
    product_id: str
    location_id: str
    name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    traffic: str | None
    provider_cost_minor: int
    provider_cost_currency: str
    selling_price_minor: int
    selling_currency: str
    billing_parameters: dict[str, object]
    provider_available: bool
    enabled: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    #: The Leaseweb credential account this offer was DISCOVERED through — a
    #: stable, non-secret id such as ``fra-account`` (never an API key, never
    #: customer-visible). One API key only sees its own Sales Organization's
    #: locations, so an offer is always owned by exactly one credential; the
    #: full ``(provider_account_id, location, product_id)`` inventory identity
    #: is durable in ``provider_routes`` (per account+location product list),
    #: and checkout pins the fulfillment account from there.
    provider_account_id: str | None = None
    #: Commercial terms: monthly VPS vs hourly cloud (checkout branches on
    #: this, never on a provider name).
    billing_model: str = BILLING_MODEL_MONTHLY
    #: Normalized customer-visible technical facts (adapters own content;
    #: never secrets, credential ids or raw API payloads).
    technical_metadata: dict[str, object] = field(default_factory=dict)
    #: Provider-neutral USD pricing audit metadata (price-source, markup-rule,
    #: FX-observed-at, provider-cost-snapshot). Durable, JSONB-safe.
    pricing_metadata: dict[str, object] = field(default_factory=dict)
    #: Explicit operator publication block (automatic publishing respects it;
    #: catalog syncs never write it).
    operator_disabled: bool = False
    #: Whether the automatic pricing policy owns the selling price (a manual
    #: price command clears it).
    auto_priced: bool = True
    #: Legacy/corrupt rows are reconstructed with their raw missing money
    #: fields for diagnostics, but are deliberately quarantined from sale.
    legacy_invalid: bool = False

    def __post_init__(self) -> None:
        if self.billing_model not in VALID_BILLING_MODELS:
            raise ValueError("billing_model is not supported")
        for boolean_name in (
            "provider_available",
            "enabled",
            "operator_disabled",
            "auto_priced",
            "legacy_invalid",
        ):
            if not isinstance(getattr(self, boolean_name), bool):
                raise ValueError(f"{boolean_name} must be boolean")
        if self.provider_account_id is not None:
            if (
                not isinstance(self.provider_account_id, str)
                or not self.provider_account_id.strip()
            ):
                raise ValueError("provider_account_id must be non-empty when supplied")
            object.__setattr__(self, "provider_account_id", self.provider_account_id.strip())
        for mapping_name in (
            "billing_parameters",
            "technical_metadata",
            "pricing_metadata",
        ):
            mapping_value = getattr(self, mapping_name)
            if not isinstance(mapping_value, dict):
                raise ValueError(f"{mapping_name} must be a mapping")
            object.__setattr__(self, mapping_name, dict(mapping_value))
        for field_name, field_value in (
            ("provider_key", self.provider_key),
            ("product_id", self.product_id),
            ("location_id", self.location_id),
            ("name", self.name),
            ("provider_cost_currency", self.provider_cost_currency),
            ("selling_currency", self.selling_currency),
        ):
            if not field_value or not str(field_value).strip():
                if self.legacy_invalid and field_name in {
                    "provider_cost_currency",
                    "selling_currency",
                }:
                    continue
                raise ValueError(f"{field_name} must not be empty")
        supported = DOMESTIC_PROVIDER_COST_CURRENCIES | GLOBAL_FIAT_CURRENCIES
        for currency_name, currency_value in (
            ("provider_cost_currency", self.provider_cost_currency),
            ("selling_currency", self.selling_currency),
        ):
            if self.legacy_invalid:
                continue
            if str(currency_value).strip().upper() not in supported:
                raise ValueError(f"{currency_name} is not an audited currency")
        if not self.legacy_invalid:
            object.__setattr__(
                self,
                "provider_cost_currency",
                str(self.provider_cost_currency).strip().upper(),
            )
            object.__setattr__(self, "selling_currency", str(self.selling_currency).strip().upper())
        for amount_name, amount_value in (
            ("provider_cost_minor", self.provider_cost_minor),
            ("selling_price_minor", self.selling_price_minor),
        ):
            if self.legacy_invalid:
                continue
            if (
                isinstance(amount_value, bool)
                or not isinstance(amount_value, int)
                or amount_value < 0
                or amount_value > 9_223_372_036_854_775_807
            ):
                raise ValueError(f"{amount_name} must be a non-negative integer")

    @property
    def sellable(self) -> bool:
        """Provider-reported, operator-visible, enabled, and explicitly priced."""
        return (
            not self.legacy_invalid
            and self.provider_available
            and self.enabled
            and not self.operator_disabled
            and not bool(self.technical_metadata.get("deprecated"))
            and not bool(self.pricing_metadata.get("fx_repricing_pending"))
            and self.selling_price_minor > 0
        )

    @property
    def ref(self) -> str:
        """Stable human-readable reference for audit metadata."""
        return f"{self.provider_key}/{self.product_id}/{self.location_id}"


def required_selling_currency(offer: SellableOffer, target_currency: str) -> str:
    """Return the only valid customer currency for this offer."""
    native = (offer.provider_cost_currency or "").strip().upper()
    return (
        native
        if native in DOMESTIC_PROVIDER_COST_CURRENCIES
        else (target_currency or "").strip().upper()
    )


def requires_currency_normalization(offer: SellableOffer, target_currency: str) -> bool:
    """Whether a sellable row violates its native or canonical currency."""
    return (offer.selling_currency or "").strip().upper() != required_selling_currency(
        offer, target_currency
    )


def _valid_pricing_metadata(
    offer: SellableOffer,
    source: str,
    target: str,
    *,
    require_exact: bool,
    catalog_stale_limit_seconds: int | None = None,
) -> bool:
    metadata = offer.pricing_metadata
    if source not in SUPPORTED_CURRENCIES or target not in SUPPORTED_CURRENCIES:
        return False
    if str(metadata.get("pricing_schema_version")) != "1":
        return False
    if str(metadata.get("target_currency", "")).upper() != target:
        return False
    if str(metadata.get("provider_cost_currency", "")).upper() != source:
        return False
    pricing_source = str(metadata.get("source_currency", "")).strip().upper()
    if pricing_source not in SUPPORTED_CURRENCIES:
        return False
    if offer.auto_priced and pricing_source != source:
        return False
    if (
        source not in DOMESTIC_PROVIDER_COST_CURRENCIES
        and pricing_source not in GLOBAL_FIAT_CURRENCIES
    ):
        return False
    try:
        if int(str(metadata.get("provider_cost_minor"))) != offer.provider_cost_minor:
            return False
        if int(str(metadata.get("final_selling_price_minor"))) != offer.selling_price_minor:
            return False
        rate = Decimal(str(metadata.get("fx_rate")))
        source_amount = Decimal(str(metadata.get("source_amount")))
        converted = Decimal(str(metadata.get("converted_cost_target_exact")))
        markup = Decimal(str(metadata.get("markup_percent", "0")))
    except (TypeError, ValueError, InvalidOperation):
        return False
    if not all(value.is_finite() and value > 0 for value in (rate, source_amount, converted)):
        return False
    if not markup.is_finite() or markup < 0:
        return False
    try:
        with localcontext() as context:
            context.prec = max(
                64,
                len(source_amount.as_tuple().digits)
                + len(rate.as_tuple().digits)
                + len(markup.as_tuple().digits)
                + 24,
            )
            expected_converted = source_amount * rate
            expected_final = major_to_minor(
                expected_converted * (Decimal(1) + markup / Decimal(100)),
                target,
                FxPurpose.CHARGE,
            )
    except (TypeError, ValueError, ArithmeticError):
        return False
    if converted != expected_converted:
        return False
    if str(metadata.get("pricing_mode", "")) not in {"auto", "manual"}:
        return False
    if str(metadata.get("rounding", "ROUND_CEILING")) != "ROUND_CEILING":
        return False
    if str(metadata.get("fx_purpose", "charge")) != "charge":
        return False
    if not isinstance(metadata.get("fx_stale"), bool):
        return False
    if not str(metadata.get("fx_provider", "")).strip():
        return False
    if not str(metadata.get("fx_source_market", "")).strip():
        return False
    try:
        provider_date = datetime.fromisoformat(str(metadata["fx_provider_date"])).date()
        observed_at = datetime.fromisoformat(str(metadata["fx_observed_at"]))
        expires_at = datetime.fromisoformat(str(metadata["fx_expires_at"]))
        valid_until = datetime.fromisoformat(str(metadata["catalog_valid_until"]))
    except (KeyError, TypeError, ValueError):
        return False
    if provider_date is None or observed_at.tzinfo is None or expires_at.tzinfo is None:
        return False
    if provider_date > datetime.now(UTC).date() or observed_at > datetime.now(UTC):
        return False
    if expires_at <= observed_at or observed_at > datetime.now(UTC):
        return False
    if valid_until.tzinfo is None or valid_until <= datetime.now(UTC):
        return False
    if catalog_stale_limit_seconds is not None:
        try:
            active_limit = int(catalog_stale_limit_seconds)
            if active_limit <= 0:
                return False
            if valid_until > observed_at + timedelta(seconds=active_limit):
                return False
        except (OverflowError, TypeError, ValueError):
            return False
    if not bool(metadata.get("fx_stale")):
        if valid_until > expires_at:
            return False
    else:
        try:
            stale_limit = int(str(metadata["fx_stale_limit_seconds"]))
        except (KeyError, TypeError, ValueError):
            return False
        if stale_limit <= 0:
            return False
        effective_limit = stale_limit
        if catalog_stale_limit_seconds is not None:
            if catalog_stale_limit_seconds <= 0:
                return False
            effective_limit = min(effective_limit, catalog_stale_limit_seconds)
        try:
            if valid_until > observed_at + timedelta(seconds=effective_limit):
                return False
        except (OverflowError, ValueError):
            return False
    if pricing_source == target:
        if str(metadata.get("fx_provider")) != "identity":
            return False
        if str(metadata.get("fx_source_market")).upper() != f"{pricing_source}/{target}":
            return False
        if rate != Decimal(1):
            return False
    elif pricing_source not in DOMESTIC_PROVIDER_COST_CURRENCIES:
        if str(metadata.get("fx_provider")) != "frankfurter":
            return False
        if str(metadata.get("fx_source_market")).upper() != f"{pricing_source}/{target}":
            return False
    if require_exact:
        key = (
            "provider_hourly_rate"
            if offer.billing_model == BILLING_MODEL_HOURLY
            else "provider_monthly_rate"
        )
        parameters = offer.billing_parameters or {}
        exact = parameters.get(key)
        if exact is None or isinstance(exact, (bool, float)):
            return False
        try:
            exact_value = Decimal(str(exact).strip())
        except (InvalidOperation, ValueError):
            return False
        if not exact_value.is_finite() or exact_value <= 0:
            return False
        try:
            if offer.provider_cost_minor != major_to_minor(exact_value, source, FxPurpose.DISPLAY):
                return False
        except (InvalidOperation, ValueError, OverflowError):
            return False
        if offer.auto_priced:
            if exact_value != source_amount:
                return False
            if str(metadata.get("source_amount_basis")) != "exact_provider_rate_major":
                return False
        else:
            if str(metadata.get("source_amount_basis")) != "manual_selling_minor_to_major":
                return False
            original_currency = str(metadata.get("original_selling_currency", "")).upper()
            if original_currency != pricing_source:
                return False
            try:
                original_minor = int(str(metadata.get("original_selling_price_minor")))
                if source_amount != minor_to_major(original_minor, pricing_source):
                    return False
            except (InvalidOperation, ValueError, TypeError, OverflowError):
                return False
        allowed_sources = {"auto", "markup"}
        if not offer.auto_priced:
            allowed_sources.add("manual")
        if str(metadata.get("price_source", metadata.get("pricing_mode"))) not in allowed_sources:
            return False
    return expected_final == offer.selling_price_minor


def has_valid_pricing_provenance(
    offer: SellableOffer,
    target_currency: str,
    *,
    catalog_stale_limit_seconds: int | None = None,
) -> bool:
    """Return whether a foreign row proves its canonical conversion.

    A positive target-currency amount without a matching FX/cost audit is
    legacy or corrupt data, not a safe basis for a new customer purchase.
    Same-currency domestic/USD rows do not need an FX record.
    """
    source = (offer.provider_cost_currency or "").strip().upper()
    target = (target_currency or "").strip().upper()
    selling = (offer.selling_currency or "").strip().upper()
    if not source or not target or not selling:
        return False
    if source == selling:
        if not offer.auto_priced:
            key = (
                "provider_hourly_rate"
                if offer.billing_model == BILLING_MODEL_HOURLY
                else "provider_monthly_rate"
            )
            parameters = offer.billing_parameters or {}
            exact = parameters.get(key)
            if exact is None or isinstance(exact, (bool, float)):
                return False
            try:
                expected = Decimal(str(exact).strip())
                if not expected.is_finite() or expected <= 0:
                    return False
                # The integer observation is a rounded audit projection. Bind
                # it to the exact text using the same half-up boundary, but do
                # not demand decimal/minor equality (sub-cent rates are valid).
                if offer.provider_cost_minor != major_to_minor(expected, source, FxPurpose.DISPLAY):
                    return False
            except (InvalidOperation, ValueError):
                return False
            return offer.selling_price_minor > 0
        return _valid_pricing_metadata(
            offer,
            source,
            source,
            require_exact=True,
            catalog_stale_limit_seconds=catalog_stale_limit_seconds,
        )
    if source in DOMESTIC_PROVIDER_COST_CURRENCIES:
        if selling != source:
            return False
        return _valid_pricing_metadata(
            offer,
            source,
            source,
            require_exact=True,
            catalog_stale_limit_seconds=catalog_stale_limit_seconds,
        )
    if selling != target:
        return False
    return _valid_pricing_metadata(
        offer,
        source,
        target,
        require_exact=True,
        catalog_stale_limit_seconds=catalog_stale_limit_seconds,
    )


def is_sellable_in_currency(
    offer: SellableOffer,
    target_currency: str,
    *,
    catalog_stale_limit_seconds: int | None = None,
) -> bool:
    """Customer gate including currency and exact FX provenance."""
    return (
        offer.sellable
        and not requires_currency_normalization(offer, target_currency)
        and has_valid_pricing_provenance(
            offer,
            target_currency,
            catalog_stale_limit_seconds=catalog_stale_limit_seconds,
        )
    )


#: The customer-facing gates, named for diagnostics/reporting.
GATE_PROVIDER_UNAVAILABLE = "provider_unavailable"
GATE_DISABLED = "disabled"
GATE_OPERATOR_DISABLED = "operator_disabled"
GATE_UNPRICED = "unpriced"
GATE_CURRENCY = "selling_currency"
GATE_PRICING_PENDING = "pricing_pending"
GATE_PRICING_PROVENANCE = "pricing_provenance"
GATE_DEPRECATED = "deprecated"


def blocking_gate(offer: SellableOffer, catalog_currency: str | None = None) -> str | None:
    """The FIRST gate keeping an offer off the storefront (None = on sale).

    Evaluated in the same order the domain documents them, so the reported
    gate is the one an operator must clear first. Pure and provider-neutral:
    this is the single definition of "why can a customer not see this".
    """
    if not offer.provider_available:
        return GATE_PROVIDER_UNAVAILABLE
    if not offer.enabled:
        return GATE_DISABLED
    if offer.operator_disabled:
        return GATE_OPERATOR_DISABLED
    if offer.technical_metadata.get("deprecated"):
        return GATE_DEPRECATED
    if offer.pricing_metadata.get("fx_repricing_pending"):
        return GATE_PRICING_PENDING
    if offer.selling_price_minor <= 0:
        return GATE_UNPRICED
    if catalog_currency is not None and requires_currency_normalization(offer, catalog_currency):
        return GATE_CURRENCY
    if catalog_currency is not None and not has_valid_pricing_provenance(offer, catalog_currency):
        return GATE_PRICING_PROVENANCE
    return None


def visibility_summary(
    offers: Iterable[SellableOffer], catalog_currency: str | None = None
) -> dict[str, int]:
    """Count offers per blocking gate (always every key, plus ``sellable``).

    Used by the operator diagnostics: an empty storefront must be explainable
    by COUNTS, not by guessing which gate failed.
    """
    summary = {
        "sellable": 0,
        GATE_PROVIDER_UNAVAILABLE: 0,
        GATE_DISABLED: 0,
        GATE_OPERATOR_DISABLED: 0,
        GATE_PRICING_PENDING: 0,
        GATE_PRICING_PROVENANCE: 0,
        GATE_DEPRECATED: 0,
        GATE_UNPRICED: 0,
    }
    if catalog_currency is not None:
        summary[GATE_CURRENCY] = 0
    for offer in offers:
        gate = blocking_gate(offer, catalog_currency)
        summary["sellable" if gate is None else gate] += 1
    return summary


@dataclass(frozen=True, slots=True)
class OfferSpecUpdate:
    """Provider-reported spec/cost refresh (catalog sync)."""

    name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    traffic: str | None
    provider_cost_minor: int
    provider_cost_currency: str
    billing_parameters: dict[str, object]
    #: Normalized technical facts (None = leave the stored value untouched).
    technical_metadata: dict[str, object] | None = None
    #: Commercial terms of the observation (None = leave stored untouched).
    billing_model: str | None = None
    provider_available: bool = True
    #: Credential account this observation came from (provenance, not a price).
    provider_account_id: str | None = None
    #: True when a read such as image availability was inconclusive. An
    #: inconclusive observation must never turn a previously sellable row off;
    #: a newly discovered row is simply kept unsellable until proven.
    provider_observation_inconclusive: bool = False

    def __post_init__(self) -> None:
        for name, value, upper in (
            ("vcpu", self.vcpu, 2_147_483_647),
            ("ram_gb", self.ram_gb, 2_147_483_647),
            ("disk_gb", self.disk_gb, 2_147_483_647),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > upper:
                raise ValueError(f"{name} must be a non-negative PostgreSQL integer")
        if (
            isinstance(self.provider_cost_minor, bool)
            or not isinstance(self.provider_cost_minor, int)
            or self.provider_cost_minor < 0
            or self.provider_cost_minor > 9_223_372_036_854_775_807
        ):
            raise ValueError("provider_cost_minor must be a non-negative signed int64 integer")
        if self.billing_model is not None and self.billing_model not in VALID_BILLING_MODELS:
            raise ValueError("billing_model is not supported")
        if self.provider_account_id is not None:
            if (
                not isinstance(self.provider_account_id, str)
                or not self.provider_account_id.strip()
            ):
                raise ValueError("provider_account_id must be non-empty when supplied")
            object.__setattr__(self, "provider_account_id", self.provider_account_id.strip())
        if not isinstance(self.billing_parameters, dict):
            raise ValueError("billing_parameters must be a mapping")
        if self.technical_metadata is not None and not isinstance(self.technical_metadata, dict):
            raise ValueError("technical_metadata must be a mapping")
        if not isinstance(self.provider_observation_inconclusive, bool):
            raise ValueError("provider_observation_inconclusive must be boolean")
        supported = DOMESTIC_PROVIDER_COST_CURRENCIES | GLOBAL_FIAT_CURRENCIES
        normalized_currency = str(self.provider_cost_currency).strip().upper()
        if normalized_currency not in supported:
            raise ValueError("provider_cost_currency is not an audited currency")
        object.__setattr__(self, "provider_cost_currency", normalized_currency)
        object.__setattr__(self, "billing_parameters", dict(self.billing_parameters))
        if self.technical_metadata is not None:
            object.__setattr__(self, "technical_metadata", dict(self.technical_metadata))


def markup_unit_price(cost_minor: int, markup_percent: int) -> int:
    """Customer price from a provider cost and an integer markup percentage.

    Integer-only by construction: ``cost * (100 + markup)`` is computed in
    minor units and rounded UP to the next minor unit so a non-zero cost can
    never be sold below cost. No float, no Decimal-to-float, per the money
    invariant.

    The selling price is normally operator-owned; this is the explicit bulk
    pricing tool an operator invokes with a markup THEY choose — never an
    automatic repricing of an existing price.
    """
    if isinstance(cost_minor, bool) or not isinstance(cost_minor, int) or cost_minor <= 0:
        raise ValueError("provider cost must be positive minor units to price from")
    if (
        isinstance(markup_percent, bool)
        or not isinstance(markup_percent, int)
        or markup_percent < 0
    ):
        raise ValueError("markup must be a non-negative integer")
    result = -((-cost_minor * (100 + markup_percent)) // 100)
    if result > 9_223_372_036_854_775_807:
        raise ValueError("marked-up price is outside signed int64 bounds")
    return result


#: Automatic pricing modes the coordinator understands. Only ``markup``
#: exists: provider cost plus an integer percentage, same-currency.
PRICING_MODE_MARKUP = "markup"


@dataclass(frozen=True, slots=True)
class PricingPolicy:
    """Server-owned automatic pricing/publication policy for one provider."""

    mode: str = PRICING_MODE_MARKUP
    markup_percent: int = 0
    auto_publish: bool = True

    def __post_init__(self) -> None:
        if self.mode != PRICING_MODE_MARKUP:
            raise ValueError("pricing policy mode must be 'markup'")
        if (
            isinstance(self.markup_percent, bool)
            or not isinstance(self.markup_percent, int)
            or self.markup_percent < 0
        ):
            raise ValueError("markup_percent must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class CatalogSyncReport:
    """One provider's catalog sync outcome, provider-neutral.

    ``verified`` holds the (product_id, location_id) pairs whose observations
    were successfully AND durably persisted this run — the ONLY rows the
    automatic pricing/publication step may touch.
    """

    provider_key: str
    ok: bool
    complete: bool
    discovered: int = 0
    persisted: int = 0
    retired: int = 0
    persistence_failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    verified: frozenset[tuple[str, str]] = frozenset()
    #: Account-qualified verified observations: (provider_account_id, product_id,
    #: location_id).  ``verified`` remains for legacy providers whose rows are
    #: intentionally unscoped; account-aware coordinators must populate this
    #: field so pricing/publication cannot alias two credential accounts.
    verified_accounts: frozenset[tuple[str, str, str]] = frozenset()
    #: Whether this source is account-qualified. An account-aware source must
    #: never fall back to pair-only verification when its qualified set is
    #: empty, because doing so can price a row through the wrong credential.
    account_aware: bool = False
    #: Billing model of the verified observations (monthly vs hourly). The
    #: coordinator only prices/publishes rows carrying this model, so two
    #: product lines can never cross-contaminate.
    billing_model: str = BILLING_MODEL_MONTHLY


class OfferCatalogSyncSource(Protocol):
    """Port for one provider's sellable-catalog sync (adapter-implemented)."""

    @property
    def provider_key(self) -> str: ...

    async def sync_catalog(self) -> CatalogSyncReport:
        """Discover, persist and reconcile; never raise for provider errors.

        A provider failure is reported in the returned report (``ok=False``),
        never as an exception, so one provider can never break another's run.
        """
        ...


@dataclass(frozen=True, slots=True)
class CatalogSyncState:
    """Persisted per-provider automatic-sync status (operator diagnostics)."""

    provider_key: str
    last_attempted_at: datetime | None = None
    last_success_at: datetime | None = None
    discovered: int = 0
    persisted: int = 0
    prices_updated: int = 0
    published: int = 0
    retired: int = 0
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class CatalogSyncStateRepository(Protocol):
    """Port for the per-provider sync status (one row per provider)."""

    async def record_run(
        self,
        *,
        provider_key: str,
        ok: bool,
        discovered: int,
        persisted: int,
        prices_updated: int,
        published: int,
        retired: int,
        warnings: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> CatalogSyncState: ...

    async def get(self, provider_key: str) -> CatalogSyncState | None: ...

    async def list_all(self) -> list[CatalogSyncState]: ...


class SellableOfferRepository(Protocol):
    """Port for sellable-offer persistence."""

    async def get(self, offer_id: UUID) -> SellableOffer | None: ...

    async def get_by_ref(
        self,
        provider_key: str,
        product_id: str,
        location_id: str,
        provider_account_id: str | None = None,
    ) -> SellableOffer | None: ...

    async def list_all(self) -> list[SellableOffer]: ...

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        """Only rows where provider_available AND enabled AND priced."""
        ...

    async def list_provider_locations(self) -> list[tuple[str, str]]:
        """Distinct (provider_key, location_id) pairs with sellable offers."""
        ...

    async def upsert_from_provider(
        self,
        *,
        provider_key: str,
        product_id: str,
        location_id: str,
        update: OfferSpecUpdate,
        provider_account_id: str | None = None,
    ) -> SellableOffer:
        """Create or refresh the row from provider sync data.

        Never touches ``enabled`` or ``selling_price_minor`` (operator-owned).
        """
        ...

    async def mark_unavailable(
        self,
        provider_key: str,
        available: Collection[tuple[str, ...]],
        billing_model: str | None = None,
        provider_account_id: str | None = None,
    ) -> int:
        """Set provider_available=False for rows of ``provider_key`` whose
        (product_id, location_id) or account-scoped tuple is not in ``available``;
        returns count.

        ``billing_model`` scopes the retirement to one commercial product
        line (a monthly sync must never retire hourly rows and vice versa);
        omitting it keeps the legacy provider-wide behavior.
        """
        ...

    async def set_enabled(self, offer_id: UUID, enabled: bool) -> SellableOffer:
        """Switch the operator visibility flag."""
        ...

    async def set_operator_disabled(self, offer_id: UUID, disabled: bool) -> SellableOffer:
        """Persist the explicit operator publication block."""
        ...

    async def set_visibility_state(
        self, offer_id: UUID, *, enabled: bool, operator_disabled: bool
    ) -> SellableOffer:
        """Atomically persist visibility and the explicit operator block."""
        ...

    async def set_auto_priced(self, offer_id: UUID, auto_priced: bool) -> SellableOffer:
        """Hand the selling price to (or take it back from) the auto policy."""
        ...

    async def set_selling_price(
        self, offer_id: UUID, selling_price_minor: int, currency: str
    ) -> SellableOffer:
        """Set the explicit customer selling price (minor units)."""
        ...

    async def set_manual_price(
        self,
        offer_id: UUID,
        selling_price_minor: int,
        currency: str,
        pricing_metadata: dict[str, object],
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_updated_at: object | None = None,
    ) -> SellableOffer | None:
        """Atomically set an operator price, manual intent, and its audit state."""
        ...

    async def set_auto_price_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        selling_price_minor: int,
        selling_currency: str,
        pricing_metadata: dict[str, object],
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """CAS-style auto-price; ``None`` means a manual/operator race won."""
        ...

    async def publish_if_current(
        self,
        offer_id: UUID,
        *,
        expected_price_minor: int,
        expected_currency: str,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """Enable only if the validated price/currency still match and no block exists."""
        ...

    async def record_auto_pricing_failure_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        expected_price_minor: int,
        expected_selling_currency: str,
        expected_pricing_metadata: dict[str, object],
        pricing_metadata: dict[str, object],
        preserve_valid_price: bool,
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """Atomically audit an FX failure and optionally clear an unsafe price."""
        ...

    async def clear_auto_price_if_current(
        self,
        offer_id: UUID,
        *,
        expected_cost_minor: int,
        expected_cost_currency: str,
        pricing_metadata: dict[str, object],
        expected_provider_rate: str | None = None,
    ) -> SellableOffer | None:
        """Leave a non-canonical auto offer unpriced after FX becomes unusable."""
        ...

    async def set_pricing_metadata(
        self, offer_id: UUID, pricing_metadata: dict[str, object]
    ) -> SellableOffer:
        """Store provider-neutral USD pricing audit metadata."""
        ...
