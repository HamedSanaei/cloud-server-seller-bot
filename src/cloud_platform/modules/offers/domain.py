"""Sellable monthly offers domain (LEASEWEB-MVP).

A **sellable offer** is one provider product at one location with TWO
explicit money snapshots: the provider cost (from catalog sync, integer
minor units) and the customer selling price (configured by an admin,
integer minor units). They are separate by design — the selling price is
NEVER derived from the provider cost, and no float ever touches either.

The row is the single gate for selling:

1. ``provider_available`` — the provider currently reports the product
   (refreshed by catalog sync);
2. ``enabled`` — the operator explicitly switched it on;
3. ``selling_price_minor > 0`` — an explicit customer price exists.

All three must hold for the customer to see and buy the offer.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    provider_available: bool = True


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

    async def set_selling_price(
        self, offer_id: UUID, selling_price_minor: int, currency: str
    ) -> SellableOffer:
        """Set the explicit customer selling price (minor units)."""
        ...
