"""ArvanCloud catalog synchronization and pricing mapper (M15-003).

Acceptance: normalized offers require no core-domain change.

The mapper converts ArvanCloud's raw ``Plan`` payload (from the provider
adapter's ``list_plans`` metadata or a raw API dict) into the provider-
neutral ``PlanPricing`` that ``PricingIngestionService`` already consumes -
exactly like Hetzner's ``_plan_pricing_from_hetzner``. The core catalog
domain (``PlanPricing`` / ``ProviderPriceEntry`` / ingestion) is untouched;
this is a provider-side adapter, the same pattern the platform requires.

Money: ArvanCloud prices are IRR. ``price_per_hour`` in the spec is a
fractional major-unit number; it is converted to integer minor units via
Decimal only (no float in money math). ``price_per_day`` /
``price_per_month`` are ingested as-is (major units, Decimal).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from cloud_platform.modules.catalog.domain import (
    PlanPricing,
    ProviderPriceEntry,
)
from cloud_platform.modules.catalog.service import PricingIngestionService
from cloud_platform.providers.arvancloud.client import ArvanCloudProvider

logger = logging.getLogger(__name__)

#: ArvanCloud's identity and billing currency (contract §7). Provider
#: metadata, not a price - all price values are ingested from the payload.
PROVIDER_KEY = "arvancloud"
CURRENCY = "IRR"


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Result of a sync operation (mirrors the Hetzner syncer's shape)."""

    total_fetched: int
    total_upserted: int
    total_skipped: int
    errors: list[str]


def _decimal_from_provider(value: Any) -> Decimal | None:
    """Parse a provider price into a Decimal major-unit value, or None.

    Accepts int/float/str numerics; rejects non-numeric and negative values
    (negative prices are a payload defect, not a discount).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, float):
            # float -> str first so we never do float money math
            decimal_value = Decimal(str(value))
        else:
            decimal_value = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if decimal_value < 0:
        return None
    return decimal_value


def _first_price(item: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = _decimal_from_provider(item.get(key))
        if value is not None:
            return value
    return None


def plan_pricing_from_arvancloud(
    item: dict[str, Any],
    *,
    location_id: str,
) -> PlanPricing:
    """Map one ArvanCloud ``Plan`` dict to provider-neutral ``PlanPricing``.

    ``location_id`` is the region the plan was fetched from - ArvanCloud
    prices are per-region, so each plan appears once per region (the catalog
    row key is (provider, plan, location)).

    Raises:
        ValueError: If the payload lacks the fields the catalog needs.
    """
    plan_id = str(item.get("id") or "").strip()
    if not plan_id:
        raise ValueError("arvancloud plan is missing id")
    region = (location_id or "").strip()
    if not region:
        raise ValueError("arvancloud plan requires a location_id (region)")

    hourly = _first_price(item, "price_per_hour")
    monthly = _first_price(item, "price_per_month")

    # At least one price is required to be sellable; the ingestion service
    # derives the hourly minor from whichever is present (hourly wins).
    price_entry = ProviderPriceEntry(
        location_id=region,
        currency=CURRENCY,
        hourly=hourly,
        monthly=monthly,
    )
    if hourly is None and monthly is None:
        raise ValueError(f"arvancloud plan {plan_id} has no hourly/monthly price")

    memory_mb = 0
    memory_bytes = item.get("memory_in_bytes")
    if memory_bytes is not None:
        memory_mb = int(Decimal(str(memory_bytes)) // (1024 * 1024))
    if not memory_mb:
        memory_mb = round(Decimal(str(item.get("memory") or 0)) * 1024)

    disk_gb = 0
    disk_bytes = item.get("disk_in_bytes")
    if disk_bytes is not None:
        disk_gb = int(Decimal(str(disk_bytes)) // (1024**3))
    if not disk_gb:
        disk_gb = int(item.get("disk") or 0)

    return PlanPricing(
        plan_id=plan_id,
        name=str(item.get("name") or plan_id),
        architecture=str(item.get("type") or item.get("category") or "unknown"),
        vcpu=int(item.get("cpu_count") or 0),
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        prices=(price_entry,),
        description=str(item.get("category") or ""),
        cpu_type=str(item.get("generation") or ""),
        storage_type=str(item.get("disk_type") or ""),
    )


class ArvanCloudCatalogSyncer:
    """Synchronizes ArvanCloud catalog data (plans per region) to the catalog.

    Mirrors ``HetznerCatalogSyncer`` in shape (``SyncResult``, ``sync_all``,
    ``build_catalog_sync_job``) but without pagination - the ArvanCloud spec
    returns a full list per region. Uses the adapter for transport so auth,
    error mapping, and throttling are shared with the operational path.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        provider: ArvanCloudProvider,
        region: str,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._region = region

    async def sync_plans(self) -> SyncResult:
        """Fetch the region's plans and ingest each into the catalog."""
        errors: list[str] = []
        total_fetched = 0
        total_upserted = 0
        total_skipped = 0

        plans = await self._provider.list_plans()
        total_fetched = len(plans)

        ingestion = PricingIngestionService(
            self._build_repo(),
        )
        for plan in plans:
            raw = _plan_to_dict(plan)
            try:
                pricing = plan_pricing_from_arvancloud(raw, location_id=self._region)
                result = await ingestion.ingest_plan(PROVIDER_KEY, pricing)
            except Exception as exc:  # per-plan isolation
                logger.error("Skipping arvancloud plan %s: %s", plan.id, exc)
                errors.append(f"plan {plan.id}: {exc}")
                continue
            for price in result.prices:
                if price.created:
                    total_upserted += 1
                else:
                    total_skipped += 1

        return SyncResult(
            total_fetched=total_fetched,
            total_upserted=total_upserted,
            total_skipped=total_skipped,
            errors=errors,
        )

    async def sync_all(self) -> dict[str, SyncResult]:
        return {"plans": await self.sync_plans()}

    def _build_repo(self) -> Any:
        from cloud_platform.modules.catalog.repository import (
            SqlAlchemyCatalogRepository,
        )

        return SqlAlchemyCatalogRepository(self._session_factory)


def _plan_to_dict(plan: Any) -> dict[str, Any]:
    """Reconstruct a raw-ish dict from a ProviderPlan for the mapper.

    The mapper is written against the raw API shape (id, name, cpu_count,
    memory_in_bytes, disk_in_bytes, price_per_hour, ...). A ProviderPlan
    carries these in ``metadata`` (see the adapter's ``list_plans``), so we
    merge the spec fields with the metadata to recover the raw shape.
    """
    raw: dict[str, Any] = {
        "id": plan.id,
        "name": plan.name,
        "type": plan.architecture,
        "cpu_count": plan.vcpu,
        "memory_in_bytes": plan.memory_mb * 1024 * 1024,
        "disk_in_bytes": plan.disk_gb * 1024**3,
    }
    metadata = getattr(plan, "metadata", {}) or {}
    for key in (
        "price_per_hour",
        "price_per_day",
        "price_per_month",
        "generation",
        "bandwidth_in_bytes",
        "category",
        "prepaid_package_template",
        "disk_type",
    ):
        if key in metadata:
            raw[key] = metadata[key]
    return raw


def build_catalog_sync_job(
    syncer: ArvanCloudCatalogSyncer,
    lock: Any,
) -> Any:
    """Wire the ArvanCloud syncer into the shared catalog sync job (M04-006)."""
    from cloud_platform.modules.catalog.domain import CatalogSyncJob, CatalogSyncStep

    async def run_plans() -> Any:
        from cloud_platform.modules.catalog.domain import CatalogSyncStepReport

        result = await syncer.sync_plans()
        return CatalogSyncStepReport(
            name="plans",
            fetched=result.total_fetched,
            upserted=result.total_upserted,
            skipped=result.total_skipped,
            errors=tuple(result.errors),
        )

    return CatalogSyncJob(
        lock,
        (CatalogSyncStep(name="plans", run=run_plans),),
    )
