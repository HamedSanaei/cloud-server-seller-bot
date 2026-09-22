"""Sellable monthly offers domain (LEASEWEB-MVP).

A **sellable offer** is one provider product at one location with TWO
explicit money snapshots: the provider cost (from catalog sync, integer
minor units) and the customer selling price (configured by an admin,
integer minor units). They are separate by design — the selling price is
NEVER derived from the provider cost, and no float ever touches either.

The row is the single gate for selling:

1. ``provider_available`` — the provider currently reports the product
   (refreshed by catalog sync);
2. ``enabled`` — the operator explicitly switched it on (automatic
   publishing may do this only while ``operator_disabled`` is false);
3. ``selling_price_minor > 0`` — an explicit customer price exists
   (owned by the automatic pricing policy while ``auto_priced`` is true,
   otherwise by the operator).

All three must hold for the customer to see and buy the offer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

#: Billing model of every offer in this module (fixed prepaid monthly).
BILLING_MODEL_PREPAID_MONTHLY = "prepaid_monthly_fixed"

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
    #: Normalized customer-visible technical facts (adapters own the content;
    #: never secrets, credential ids or raw API payloads).
    technical_metadata: dict[str, object] = field(default_factory=dict)
    #: Explicit operator publication block (automatic publishing respects it;
    #: catalog syncs never write it).
    operator_disabled: bool = False
    #: Whether the automatic pricing policy owns the selling price (a manual
    #: price command clears it).
    auto_priced: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_key", self.provider_key),
            ("product_id", self.product_id),
            ("location_id", self.location_id),
            ("name", self.name),
            ("provider_cost_currency", self.provider_cost_currency),
            ("selling_currency", self.selling_currency),
        ):
            if not value or not str(value).strip():
                raise ValueError(f"{name} must not be empty")
        if self.provider_cost_minor < 0 or self.selling_price_minor < 0:
            raise ValueError("prices must not be negative")

    @property
    def sellable(self) -> bool:
        """All three gates: provider-reported, enabled, explicitly priced."""
        return self.provider_available and self.enabled and self.selling_price_minor > 0

    @property
    def ref(self) -> str:
        """Stable human-readable reference for audit metadata."""
        return f"{self.provider_key}/{self.product_id}/{self.location_id}"


#: The three gates, named for diagnostics/reporting (never customer-facing).
GATE_PROVIDER_UNAVAILABLE = "provider_unavailable"
GATE_DISABLED = "disabled"
GATE_UNPRICED = "unpriced"


def blocking_gate(offer: SellableOffer) -> str | None:
    """The FIRST gate keeping an offer off the storefront (None = on sale).

    Evaluated in the same order the domain documents them, so the reported
    gate is the one an operator must clear first. Pure and provider-neutral:
    this is the single definition of "why can a customer not see this".
    """
    if not offer.provider_available:
        return GATE_PROVIDER_UNAVAILABLE
    if not offer.enabled:
        return GATE_DISABLED
    if offer.selling_price_minor <= 0:
        return GATE_UNPRICED
    return None


def visibility_summary(offers: Iterable[SellableOffer]) -> dict[str, int]:
    """Count offers per blocking gate (always every key, plus ``sellable``).

    Used by the operator diagnostics: an empty storefront must be explainable
    by COUNTS, not by guessing which gate failed.
    """
    summary = {
        "sellable": 0,
        GATE_PROVIDER_UNAVAILABLE: 0,
        GATE_DISABLED: 0,
        GATE_UNPRICED: 0,
    }
    for offer in offers:
        gate = blocking_gate(offer)
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
    provider_available: bool = True
    #: Credential account this observation came from (provenance, not a price).
    provider_account_id: str | None = None


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
    if cost_minor <= 0:
        raise ValueError("provider cost must be positive minor units to price from")
    if markup_percent < 0:
        raise ValueError("markup must not be negative")
    return -((-cost_minor * (100 + markup_percent)) // 100)


#: Automatic pricing modes the coordinator understands. Only ``markup``
#: exists: provider cost plus an integer percentage, same-currency.
PRICING_MODE_MARKUP = "markup"


@dataclass(frozen=True, slots=True)
class PricingPolicy:
    """Server-owned automatic pricing/publication policy for one provider."""

    mode: str = PRICING_MODE_MARKUP
    markup_percent: int = 0
    auto_publish: bool = True


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
        self, provider_key: str, product_id: str, location_id: str
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
    ) -> SellableOffer:
        """Create or refresh the row from provider sync data.

        Never touches ``enabled`` or ``selling_price_minor`` (operator-owned).
        """
        ...

    async def mark_unavailable(self, provider_key: str, available: set[tuple[str, str]]) -> int:
        """Set provider_available=False for rows of ``provider_key`` whose
        (product_id, location_id) is not in ``available``; returns count."""
        ...

    async def set_enabled(self, offer_id: UUID, enabled: bool) -> SellableOffer:
        """Switch the operator visibility flag."""
        ...

    async def set_operator_disabled(self, offer_id: UUID, disabled: bool) -> SellableOffer:
        """Persist the explicit operator publication block."""
        ...

    async def set_auto_priced(self, offer_id: UUID, auto_priced: bool) -> SellableOffer:
        """Hand the selling price to (or take it back from) the auto policy."""
        ...

    async def set_selling_price(
        self, offer_id: UUID, selling_price_minor: int, currency: str
    ) -> SellableOffer:
        """Set the explicit customer selling price (minor units)."""
        ...
