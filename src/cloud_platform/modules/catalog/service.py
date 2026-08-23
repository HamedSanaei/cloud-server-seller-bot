"""Pricing ingestion service (M04-005).

Turns a provider's location-aware plan pricing into persistent catalog rows.
Prices come exclusively from the provider payload — nothing is hard-coded —
and all money math stays in Decimal integer minor units.
"""

from __future__ import annotations

import logging

from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.catalog.domain import (
    CatalogEntrySpec,
    CatalogError,
    CatalogRepository,
    IngestedPrice,
    IngestedPricing,
    OfferNotFoundError,
    OfferRef,
    OfferState,
    PlanPricing,
    PricingError,
    hourly_minor,
)
from cloud_platform.modules.users.domain import Permission, PermissionChecker, User

logger = logging.getLogger(__name__)


class PricingIngestionService:
    """Persists a plan's per-location prices to the catalog."""

    def __init__(self, catalog_repo: CatalogRepository) -> None:
        self._catalog = catalog_repo

    async def ingest_plan(self, provider_key: str, plan: PlanPricing) -> IngestedPricing:
        """Upsert one catalog row per (plan, location).

        Raises:
            PricingError: If the provider key is empty.
            ValueError: On invalid plan/price data (via domain validation).
        """
        if not provider_key or not provider_key.strip():
            raise PricingError("provider_key must not be empty")

        prices: list[IngestedPrice] = []
        for entry in plan.prices:
            spec = CatalogEntrySpec(
                provider_key=provider_key,
                plan_id=plan.plan_id,
                location_id=entry.location_id,
                name=plan.name,
                architecture=plan.architecture,
                vcpu=plan.vcpu,
                memory_mb=plan.memory_mb,
                disk_gb=plan.disk_gb,
                currency=entry.currency,
                price_per_quantum=hourly_minor(entry),
                description=plan.description,
                extra_metadata={
                    "cpu_type": plan.cpu_type,
                    "storage_type": plan.storage_type,
                },
            )
            created = await self._catalog.upsert_entry(spec)
            prices.append(
                IngestedPrice(
                    location_id=entry.location_id,
                    currency=entry.currency,
                    hourly_minor=spec.price_per_quantum,
                    created=created,
                )
            )

        result = IngestedPricing(plan_id=plan.plan_id, prices=tuple(prices))
        logger.info(
            "ingested %d location prices for %s/%s",
            len(prices),
            provider_key,
            plan.plan_id,
        )
        return result


class OfferVisibilityService:
    """Admin-controlled visibility of sellable offers (M04-007).

    An offer is a (provider, plan, location) combination. Hiding it makes it
    unavailable for sale without deleting it (prices and history are kept).
    Every change is admin-authorized and audited with a non-empty reason.
    """

    def __init__(
        self,
        catalog_repo: CatalogRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self._catalog = catalog_repo
        self._audit = AuditTrail(audit_repo)

    async def set_offer_visibility(
        self,
        *,
        ref: OfferRef,
        enabled: bool,
        actor: User,
        reason: str,
    ) -> OfferState:
        """Show (``enabled=True``) or hide (``enabled=False``) one offer.

        Idempotent: setting the state the offer already has changes nothing
        and records no audit event.

        Raises:
            PermissionDeniedError: If the actor lacks admin:manage_settings.
            CatalogError: On an empty reason.
            OfferNotFoundError: If the combination is not in the catalog.
        """
        PermissionChecker(actor).require(Permission.ADMIN_MANAGE_SETTINGS)
        if not reason or not reason.strip():
            raise CatalogError("offer visibility changes must carry a non-empty reason")

        current = await self._catalog.get_offer(ref)
        if current is None:
            raise OfferNotFoundError(f"offer {ref.key} not found")

        if current.enabled is enabled:
            return current  # idempotent no-op

        await self._catalog.set_offer_enabled(ref, enabled)
        action = "catalog.offer_show" if enabled else "catalog.offer_hide"
        await self._audit.record_mutation(
            actor_type=ActorType.ADMIN,
            actor_id=actor.id,
            action=action,
            resource_type="catalog_offer",
            resource_id=ref.key,
            reason=reason,
            metadata={
                "provider": ref.provider_key,
                "plan": ref.plan_id,
                "location": ref.location_id,
            },
        )
        logger.info("offer %s now enabled=%s", ref.key, enabled)
        return OfferState(
            id=current.id,
            ref=ref,
            name=current.name,
            enabled=enabled,
            price_per_quantum=current.price_per_quantum,
            currency=current.currency,
        )
