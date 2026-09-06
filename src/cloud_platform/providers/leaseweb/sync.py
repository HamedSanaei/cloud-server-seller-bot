"""LeaseWeb catalog synchronization and pricing mapper.

Mirrors the Hetzner syncer shape (``SyncResult``, ``sync_all``,
``build_catalog_sync_job``) so the container and ops tooling treat every
provider identically. Prices are LeaseWeb-reported per-instance-type values;
mapping is Decimal-only, never float, never hard-coded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from cloud_platform.modules.catalog.domain import PlanPricing, ProviderPriceEntry
from cloud_platform.modules.catalog.service import PricingIngestionService
from cloud_platform.providers.leaseweb.client import LeaseWebProvider

logger = logging.getLogger(__name__)

PROVIDER_KEY = "leaseweb"
CURRENCY = "EUR"


@dataclass(frozen=True, slots=True)
class SyncResult:
    total_fetched: int
    total_upserted: int
    total_skipped: int
    errors: list[str]


def _decimal_from_provider(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if decimal_value < 0:
        return None
    return decimal_value


def plan_pricing_from_leaseweb(item: dict[str, Any], *, location_id: str) -> PlanPricing:
    """Map one LeaseWeb instanceType dict to provider-neutral ``PlanPricing``."""
    plan_id = str(item.get("id") or item.get("name") or "").strip()
    if not plan_id:
        raise ValueError("leaseweb plan is missing id")
    region = (location_id or "").strip()
    if not region:
        raise ValueError("leaseweb plan requires a location_id (region)")

    hourly = _decimal_from_provider(item.get("pricePerHour"))
    monthly = _decimal_from_provider(item.get("pricePerMonth"))
    price_entry = ProviderPriceEntry(
        location_id=region,
        currency=str(item.get("currency") or CURRENCY),
        hourly=hourly,
        monthly=monthly,
    )

    raw_resources = item.get("resources")
    resources: dict[str, Any] = raw_resources if isinstance(raw_resources, dict) else {}
    return PlanPricing(
        plan_id=plan_id,
        name=str(item.get("name") or plan_id),
        architecture=str(item.get("architecture") or "x86"),
        vcpu=int(item.get("cpu") or resources.get("cpu") or 0),
        memory_mb=int(item.get("memoryMb") or resources.get("memoryMb") or 0),
        disk_gb=int(item.get("disk") or resources.get("disk") or 0),
        prices=(price_entry,),
        description=str(item.get("description") or ""),
        cpu_type=str(item.get("cpuType") or ""),
        storage_type=str(item.get("storageType") or ""),
    )


class LeaseWebCatalogSyncer:
    """Synchronizes LeaseWeb catalog data (regions + instance types) to the catalog."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        provider: LeaseWebProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    async def sync_locations(self) -> SyncResult:
        from sqlalchemy import select

        from cloud_platform.db.base import Provider, ProviderLocation
        from cloud_platform.modules.catalog.repository import provider_key_to_uuid

        errors: list[str] = []
        try:
            locations = await self._provider.list_locations()
        except Exception as exc:
            logger.error("LeaseWeb locations sync failed: %s", exc)
            return SyncResult(0, 0, 0, [str(exc)])
        upserted = 0
        skipped = 0
        async with self._session_factory() as session:
            existing = (
                (await session.execute(select(Provider.id).where(Provider.name == PROVIDER_KEY)))
                .scalars()
                .first()
            )
            if existing is None:
                provider_id = provider_key_to_uuid(PROVIDER_KEY)
                session.add(Provider(id=provider_id, name=PROVIDER_KEY, region="global"))
                await session.flush()
            else:
                provider_id = existing
            for loc in locations:
                stmt = select(ProviderLocation).where(
                    ProviderLocation.provider_id == provider_id,
                    ProviderLocation.location_id == loc.id,
                )
                found = (await session.execute(stmt)).scalars().first()
                if found:
                    found.name = loc.name
                    found.country_code = loc.country_code
                    found.city = loc.city
                    found.network_zone = loc.network_zone
                    skipped += 1
                else:
                    session.add(
                        ProviderLocation(
                            provider_id=provider_id,
                            location_id=loc.id,
                            name=loc.name,
                            country_code=loc.country_code,
                            city=loc.city,
                            network_zone=loc.network_zone,
                        )
                    )
                    upserted += 1
            await session.commit()
        return SyncResult(len(locations), upserted, skipped, errors)

    async def sync_plans(self) -> SyncResult:
        errors: list[str] = []
        total_fetched = 0
        total_upserted = 0
        total_skipped = 0
        try:
            locations = await self._provider.list_locations()
        except Exception as exc:
            return SyncResult(0, 0, 0, [f"locations: {exc}"])
        region_ids = [loc.id for loc in locations] or ["default"]
        try:
            plans = await self._provider.list_plans()
        except Exception as exc:
            return SyncResult(0, 0, 0, [f"plans: {exc}"])
        total_fetched = len(plans) * len(region_ids)
        from cloud_platform.modules.catalog.repository import SqlAlchemyCatalogRepository

        ingestion = PricingIngestionService(SqlAlchemyCatalogRepository(self._session_factory))
        for plan in plans:
            raw = _plan_to_dict(plan)
            for region in region_ids:
                try:
                    pricing = plan_pricing_from_leaseweb(raw, location_id=region)
                    result = await ingestion.ingest_plan(PROVIDER_KEY, pricing)
                except Exception as exc:
                    logger.error("Skipping leaseweb plan %s/%s: %s", plan.id, region, exc)
                    errors.append(f"plan {plan.id}/{region}: {exc}")
                    continue
                for price in result.prices:
                    if price.created:
                        total_upserted += 1
                    else:
                        total_skipped += 1
        return SyncResult(total_fetched, total_upserted, total_skipped, errors)

    async def sync_all(self) -> dict[str, SyncResult]:
        locations = await self.sync_locations()
        plans = await self.sync_plans()
        return {"locations": locations, "plans": plans}


def _plan_to_dict(plan: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"id": plan.id, "name": plan.name, "architecture": plan.architecture}
    metadata = getattr(plan, "metadata", {}) or {}
    for key in ("price_per_hour", "price_per_month", "currency", "region"):
        if key in metadata:
            raw_key = {"price_per_hour": "pricePerHour", "price_per_month": "pricePerMonth"}.get(
                key, key
            )
            raw[raw_key] = metadata[key]
    raw["cpu"] = plan.vcpu
    raw["memoryMb"] = plan.memory_mb
    raw["disk"] = plan.disk_gb
    return raw


def build_catalog_sync_job(syncer: LeaseWebCatalogSyncer, lock: Any) -> Any:
    """Wire the LeaseWeb syncer into the shared catalog sync job (M04-006)."""
    from cloud_platform.modules.catalog.domain import CatalogSyncJob, CatalogSyncStep

    async def run_locations() -> Any:
        from cloud_platform.modules.catalog.domain import CatalogSyncStepReport

        result = await syncer.sync_locations()
        return CatalogSyncStepReport(
            name="leaseweb-locations",
            fetched=result.total_fetched,
            upserted=result.total_upserted,
            skipped=result.total_skipped,
            errors=tuple(result.errors),
        )

    async def run_plans() -> Any:
        from cloud_platform.modules.catalog.domain import CatalogSyncStepReport

        result = await syncer.sync_plans()
        return CatalogSyncStepReport(
            name="leaseweb-plans",
            fetched=result.total_fetched,
            upserted=result.total_upserted,
            skipped=result.total_skipped,
            errors=tuple(result.errors),
        )

    return CatalogSyncJob(
        lock,
        (
            CatalogSyncStep(name="leaseweb-locations", run=run_locations),
            CatalogSyncStep(name="leaseweb-plans", run=run_plans),
        ),
    )


# Back-compat alias (same reason as the client alias above).
LeasewebCatalogSyncer = LeaseWebCatalogSyncer
