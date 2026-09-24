"""Offer administration service (LEASEWEB-MVP).

Every money-affecting or visibility mutation is admin-authorized
(``admin:manage_settings``), requires a non-empty reason, is audited, and is
idempotent. Selling prices are stored as explicit integer minor units.
Automatic pricing may derive them only through the audited FX/markup pipeline;
manual prices remain operator-owned and are never silently overwritten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.catalog.domain import LocationRecord, LocationRepository
from cloud_platform.modules.fx.domain import minor_to_major
from cloud_platform.modules.offers.domain import (
    OfferError,
    OfferNotFoundError,
    SellableOffer,
    SellableOfferRepository,
    has_valid_pricing_provenance,
    is_sellable_in_currency,
    required_selling_currency,
    requires_currency_normalization,
)
from cloud_platform.modules.users.domain import Permission, PermissionChecker, User

logger = logging.getLogger(__name__)


class OfferAdminError(OfferError):
    """Base error for offer administration."""


class OfferAdminService:
    """Admin commands over the sellable-offer price book."""

    def __init__(
        self,
        offers_repo: SellableOfferRepository,
        audit_repo: AuditRepository,
        catalog_currency: str = "USD",
    ) -> None:
        self._offers = offers_repo
        self._audit = AuditTrail(audit_repo)
        self._catalog_currency = catalog_currency.strip().upper()

    @staticmethod
    def _require_reason(reason: str) -> None:
        if not reason or not reason.strip():
            raise OfferAdminError("offer changes must carry a non-empty reason")

    @staticmethod
    def _authorize(actor: User) -> None:
        PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)

    async def set_selling_price(
        self,
        *,
        actor: User,
        offer_id: UUID,
        selling_price_minor: int,
        currency: str,
        reason: str,
    ) -> SellableOffer:
        """Set the explicit customer monthly price (minor units)."""
        self._authorize(actor)
        self._require_reason(reason)
        if selling_price_minor <= 0:
            raise OfferAdminError("selling price must be positive minor units")
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise OfferAdminError("selling currency must be a 3-letter ISO code")

        offer = await self._offers.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"offer {offer_id} not found")
        required_currency = required_selling_currency(offer, self._catalog_currency)
        if currency != required_currency:
            raise OfferAdminError(
                f"offer selling currency must be {required_currency}; "
                "normalize deliberately instead of relabelling"
            )
        if (
            offer.provider_cost_currency.strip().upper() != self._catalog_currency
            and offer.provider_cost_currency.strip().upper() not in ("IRT", "IRR")
        ):
            raise OfferAdminError(
                "foreign offers must be normalized with the catalog FX command; "
                "manual target-currency prices require audited FX provenance"
            )
        updated = await self._offers.set_manual_price(
            offer_id,
            selling_price_minor,
            currency,
            {
                "pricing_schema_version": 1,
                "price_source": "manual",
                "pricing_mode": "manual",
                "source_currency": currency,
                "source_amount": str(minor_to_major(selling_price_minor, currency)),
                "target_currency": currency,
                "provider_cost_minor": offer.provider_cost_minor,
                "provider_cost_currency": offer.provider_cost_currency,
                "markup_percent": "0",
                "rounding": "operator_minor_units",
                "final_selling_price_minor": selling_price_minor,
            },
            expected_cost_minor=offer.provider_cost_minor,
            expected_cost_currency=offer.provider_cost_currency,
            expected_updated_at=offer.updated_at,
        )
        if updated is None:
            raise OfferAdminError("provider cost changed while setting the manual price; retry")
        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=actor.id,
            action="offers.set_price",
            resource_type="sellable_offer",
            resource_id=str(offer_id),
            reason=reason,
            metadata={
                "offer": updated.ref,
                "selling_price_minor": str(selling_price_minor),
                "currency": currency,
                "provider_cost_minor": str(updated.provider_cost_minor),
                "provider_cost_currency": updated.provider_cost_currency,
            },
        )
        logger.info("offer %s priced at %d %s", updated.ref, selling_price_minor, currency)
        return updated

    async def set_enabled(
        self, *, actor: User, offer_id: UUID, enabled: bool, reason: str
    ) -> SellableOffer:
        """Show or hide one offer (idempotent; never deletes history).

        Hiding records an explicit operator block that automatic publishing
        never undoes; showing clears it.
        """
        self._authorize(actor)
        self._require_reason(reason)

        offer = await self._offers.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"offer {offer_id} not found")
        if enabled and requires_currency_normalization(offer, self._catalog_currency):
            raise OfferAdminError(
                f"offer must be normalized to {self._catalog_currency} before it can be enabled"
            )
        if enabled:
            metadata = offer.pricing_metadata or {}
            if (
                not offer.provider_available
                or offer.selling_price_minor <= 0
                or bool(metadata.get("fx_repricing_pending"))
                or offer.technical_metadata.get("deprecated")
                or not has_valid_pricing_provenance(offer, self._catalog_currency)
            ):
                raise OfferAdminError(
                    "offer needs valid current pricing provenance before it can be shown"
                )
        if offer.enabled is enabled and offer.operator_disabled is not enabled:
            return offer  # idempotent no-op
        updated = await self._offers.set_visibility_state(
            offer_id,
            enabled=enabled,
            operator_disabled=not enabled,
        )
        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=actor.id,
            action="offers.show" if enabled else "offers.hide",
            resource_type="sellable_offer",
            resource_id=str(offer_id),
            reason=reason,
            metadata={
                "offer": updated.ref,
                "enabled": str(enabled).lower(),
                "operator_disabled": str(not enabled).lower(),
            },
        )
        logger.info("offer %s enabled=%s", updated.ref, enabled)
        return updated

    async def list_all_rows(self) -> list[SellableOffer]:
        """Every offer row (inspection, read-only)."""
        return await self._offers.list_all()

    async def list_sellable(self) -> list[SellableOffer]:
        """Only what customers can currently buy in the canonical currency."""
        return [
            offer
            for offer in await self._offers.list_sellable()
            if is_sellable_in_currency(offer, self._catalog_currency)
        ]


@dataclass(frozen=True, slots=True)
class OfferBrowseLocation:
    """One location with sellable offers (customer browse view)."""

    location_id: str
    name: str
    country_code: str | None
    city: str | None
    offers: tuple[SellableOffer, ...]


class OfferBrowseService:
    """Customer-facing browse over the sellable offer price book.

    Only rows passing ALL THREE gates (provider-reported, enabled, priced)
    are ever visible; the location display metadata comes from the synced
    provider-location rows when present, else the location code.
    """

    def __init__(
        self,
        offers_repo: SellableOfferRepository,
        location_repo: LocationRepository,
        catalog_currency: str = "USD",
    ) -> None:
        self._offers = offers_repo
        self._locations = location_repo
        self._catalog_currency = catalog_currency.strip().upper()

    async def list_locations(self) -> list[OfferBrowseLocation]:
        offers = [
            offer
            for offer in await self._offers.list_sellable()
            if is_sellable_in_currency(offer, self._catalog_currency)
        ]
        if not offers:
            return []
        index: dict[tuple[str, str], LocationRecord] = {}
        for key in sorted({o.provider_key for o in offers}):
            for record in await self._locations.list_for_provider(key):
                index[(key, record.location_id)] = record
        by_location: dict[tuple[str, str], list[SellableOffer]] = {}
        for offer in offers:
            by_location.setdefault((offer.provider_key, offer.location_id), []).append(offer)
        views: list[OfferBrowseLocation] = []
        for (provider_key, location_id), rows in sorted(by_location.items()):
            loc_record = index.get((provider_key, location_id))
            views.append(
                OfferBrowseLocation(
                    location_id=location_id,
                    name=loc_record.name if loc_record else location_id,
                    country_code=loc_record.country_code if loc_record else None,
                    city=loc_record.city if loc_record else None,
                    offers=tuple(sorted(rows, key=lambda o: o.name)),
                )
            )
        return views
