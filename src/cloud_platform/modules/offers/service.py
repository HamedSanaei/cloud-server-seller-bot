"""Offer administration service (LEASEWEB-MVP).

Every money-affecting or visibility mutation is admin-authorized
(``admin:manage_settings``), requires a non-empty reason, is audited, and is
idempotent. Selling prices are stored as explicit integer minor units —
never derived from provider cost, never float.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.catalog.domain import LocationRecord, LocationRepository
from cloud_platform.modules.offers.domain import (
    OfferError,
    OfferNotFoundError,
    SellableOffer,
    SellableOfferRepository,
)
from cloud_platform.modules.users.domain import Permission, PermissionChecker, User

logger = logging.getLogger(__name__)


class OfferAdminError(OfferError):
    """Base error for offer administration."""


class OfferAdminService:
    """Admin commands over the sellable-offer price book."""

    def __init__(self, offers_repo: SellableOfferRepository, audit_repo: AuditRepository) -> None:
        self._offers = offers_repo
        self._audit = AuditTrail(audit_repo)

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

        offer = await self._offers.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"offer {offer_id} not found")
        updated = await self._offers.set_selling_price(offer_id, selling_price_minor, currency)
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
        """Show or hide one offer (idempotent; never deletes history)."""
        self._authorize(actor)
        self._require_reason(reason)

        offer = await self._offers.get(offer_id)
        if offer is None:
            raise OfferNotFoundError(f"offer {offer_id} not found")
        if offer.enabled is enabled:
            return offer  # idempotent no-op
        updated = await self._offers.set_enabled(offer_id, enabled)
        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=actor.id,
            action="offers.show" if enabled else "offers.hide",
            resource_type="sellable_offer",
            resource_id=str(offer_id),
            reason=reason,
            metadata={"offer": updated.ref, "enabled": str(enabled).lower()},
        )
        logger.info("offer %s enabled=%s", updated.ref, enabled)
        return updated

    async def list_all_rows(self) -> list[SellableOffer]:
        """Every offer row (inspection, read-only)."""
        return await self._offers.list_all()

    async def list_sellable(self) -> list[SellableOffer]:
        """Only what customers can currently buy."""
        return await self._offers.list_sellable()


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
    ) -> None:
        self._offers = offers_repo
        self._locations = location_repo

    async def list_locations(self) -> list[OfferBrowseLocation]:
        offers = await self._offers.list_sellable()
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
