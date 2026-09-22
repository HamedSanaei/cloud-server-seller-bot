"""Hourly cloud catalog sync (STOREFRONT-REWORK).

Turns Leaseweb Public Cloud regions and instance types into hourly
``SellableOffer`` rows through the official read-only APIs: regions are
authoritative for location membership, the region-scoped instance-type list
for plan membership at that region. A region that fails is isolated — its
offers are left untouched rather than retired — while every other region
still syncs. Availability is reconciled only when every discovered region
synced cleanly, so a partial view never mass-retires unknown inventory.

``enabled`` and ``selling_price_minor`` are NEVER written here: the
automatic pricing/publication policy owns them downstream, and an explicit
operator block is never touched. Nothing is ever deleted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.offers.domain import OfferSpecUpdate, TechnicalSpec
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.providers.errors import ProviderError
from cloud_platform.providers.leaseweb.cloud import (
    PROVIDER_KEY,
    CloudInstanceType,
    LeasewebHourlyCloudProvider,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegionTypeReport:
    """How many hourly plans one region contributed (or why it did not)."""

    region_id: str
    products: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CloudSyncResult:
    """Outcome of one hourly-cloud sync run."""

    regions: tuple[RegionTypeReport, ...] = ()
    offers_written: int = 0
    marked_unavailable: int = 0
    warnings: tuple[str, ...] = ()
    verified: frozenset[tuple[str, str]] = frozenset()
    persistence_failures: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)


def _technical_spec(item: CloudInstanceType) -> dict[str, object]:
    return TechnicalSpec(
        architecture=item.architecture,
        cpu_type=item.cpu_type,
        storage_type=item.storage_type,
        ipv4=item.ipv4,
        ipv6=item.ipv6,
    ).to_metadata() | {
        "plan_family": item.family_key,
        "plan_family_name": item.family_name,
    }


class LeasewebHourlyCloudSyncer:
    """Syncs hourly instance types of every region into the price book."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        provider: LeasewebHourlyCloudProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    async def sync_all(self) -> CloudSyncResult:
        """Regions, then per-region instance types, then reconciliation."""
        from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

        offers_repo = SqlAlchemySellableOfferRepository(self._session_factory)
        locations_repo = SqlAlchemyLocationRepository(self._session_factory)
        warnings: list[str] = []
        errors: list[str] = []
        persistence_failures: list[str] = []
        reports: list[RegionTypeReport] = []
        verified: set[tuple[str, str]] = set()
        available: set[tuple[str, str]] = set()
        written = 0

        try:
            regions = await self._provider.list_regions()
        except Exception as exc:
            errors.append(f"regions: {type(exc).__name__}: {exc}")
            return CloudSyncResult(errors=errors)
        if not regions:
            errors.append("provider returned no readable regions")
            return CloudSyncResult(errors=errors)

        for region in regions:
            try:
                await locations_repo.upsert(
                    LocationRecord(
                        provider_key=PROVIDER_KEY,
                        location_id=region.id,
                        name=region.name,
                        country_code=region.country_code,
                        city=region.city,
                    )
                )
            except Exception as exc:
                warnings.append(f"location metadata {region.id}: {type(exc).__name__}")
            try:
                types = await self._provider.list_instance_types(region.id)
            except ProviderError as exc:
                reports.append(
                    RegionTypeReport(region_id=region.id, products=0, error=type(exc).__name__)
                )
                warnings.append(f"region {region.id}: {type(exc).__name__}")
                continue
            counted = 0
            region_failed = False
            for item in types:
                pair = (item.id, region.id)
                update = OfferSpecUpdate(
                    name=item.name,
                    vcpu=item.vcpu,
                    ram_gb=item.ram_gb,
                    disk_gb=item.disk_gb,
                    traffic=item.traffic,
                    provider_cost_minor=item.hourly_cost_minor,
                    provider_cost_currency=item.currency,
                    billing_parameters={
                        "contract_type": "HOURLY",
                        "monthly_estimate_source": "hourly_rate",
                        "instance_type_id": item.id,
                        "region": region.id,
                    },
                    technical_metadata=_technical_spec(item),
                    billing_model="hourly",
                    provider_available=True,
                )
                try:
                    await offers_repo.upsert_from_provider(
                        provider_key=PROVIDER_KEY,
                        product_id=item.id,
                        location_id=region.id,
                        update=update,
                    )
                except Exception as exc:
                    persistence_failures.append(f"upsert {item.id}/{region.id}: {exc}")
                    warnings.append(
                        f"{region.id}: keeping last-known offers "
                        f"({type(exc).__name__}); nothing retired"
                    )
                    region_failed = True
                    continue
                available.add(pair)
                verified.add(pair)
                counted += 1
                written += 1
            reports.append(
                RegionTypeReport(
                    region_id=region.id,
                    products=counted,
                    error="persistence failure" if region_failed else None,
                )
            )

        marked = 0
        if not any(report.error for report in reports) and regions:
            try:
                marked = await offers_repo.mark_unavailable(
                    PROVIDER_KEY, available, billing_model="hourly"
                )
            except Exception as exc:
                persistence_failures.append(f"mark_unavailable: {exc}")
        else:
            warnings.append("skipped mark_unavailable: current availability unreadable")
        return CloudSyncResult(
            regions=tuple(reports),
            offers_written=written,
            marked_unavailable=marked,
            warnings=tuple(warnings),
            verified=frozenset(verified),
            persistence_failures=tuple(persistence_failures),
            errors=errors,
        )
