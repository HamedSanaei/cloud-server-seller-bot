"""Leaseweb ordering-VPS catalog sync (LEASEWEB-MVP).

For every configured location (``LEASEWEB_LOCATIONS``):

- locations are upserted into ``provider_locations`` (display metadata:
  country/city from the operator display map in the ordering client);
- every ordering product is refreshed into ``sellable_offers`` with its
  CURRENT provider monthly cost at the configured contract/billing terms;

Sync NEVER touches the operator-owned fields (``enabled``,
``selling_price_minor``) — it only refreshes provider-reported data and the
``provider_available`` flag. Products Leaseweb no longer reports for a
location are flagged unavailable, which automatically removes them from the
customer browse view without deleting history.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.offers.domain import OfferSpecUpdate
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.providers.leaseweb.ordering import LeaseWebOrderingProvider

logger = logging.getLogger(__name__)

PROVIDER_KEY = "leaseweb"
CURRENCY = "EUR"


@dataclass(frozen=True, slots=True)
class SyncResult:
    total_fetched: int
    total_upserted: int
    total_skipped: int
    errors: list[str]


class LeaseWebOrderingCatalogSyncer:
    """Syncs ordering products into the sellable-offer price book."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        provider: LeaseWebOrderingProvider,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider

    async def sync_locations(self) -> SyncResult:
        from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

        repo = SqlAlchemyLocationRepository(self._session_factory)
        errors: list[str] = []
        upserted = 0
        skipped = 0
        fetched = 0
        try:
            locations = await self._provider.list_locations()
        except Exception as exc:
            logger.error("Leaseweb ordering locations sync failed: %s", exc)
            return SyncResult(0, 0, 0, [f"locations: {exc}"])
        for loc in locations:
            fetched += 1
            try:
                created = await repo.upsert(
                    LocationRecord(
                        provider_key=PROVIDER_KEY,
                        location_id=loc.id,
                        name=loc.name,
                        country_code=loc.country_code,
                        city=loc.city,
                    )
                )
            except Exception as exc:
                errors.append(f"location {loc.id}: {exc}")
                continue
            upserted += created
            skipped += 0 if created else 1
        return SyncResult(fetched, upserted, skipped, errors)

    async def sync_products(self) -> SyncResult:
        offers_repo = SqlAlchemySellableOfferRepository(self._session_factory)
        errors: list[str] = []
        fetched = 0
        upserted = 0
        skipped = 0
        available: set[tuple[str, str]] = set()
        try:
            locations = await self._provider.list_locations()
        except Exception as exc:
            return SyncResult(0, 0, 0, [f"locations: {exc}"])
        for location in locations:
            try:
                products = await self._provider.list_products(location.id)
            except Exception as exc:
                errors.append(f"products {location.id}: {exc}")
                continue
            for product in products:
                fetched += 1
                try:
                    detail = await self._provider.get_product(location.id, product.id)
                except Exception as exc:
                    errors.append(f"detail {product.id}/{location.id}: {exc}")
                    continue
                available.add((product.id, location.id))
                offer_product = detail.product
                if detail.available_locations and location.id not in detail.available_locations:
                    # Product no longer sold at this location: flag unavailable.
                    try:
                        await offers_repo.upsert_from_provider(
                            provider_key=PROVIDER_KEY,
                            product_id=product.id,
                            location_id=location.id,
                            update=OfferSpecUpdate(
                                name=product.name,
                                vcpu=product.vcpu,
                                ram_gb=product.ram_gb,
                                disk_gb=product.disk_gb,
                                traffic=product.traffic,
                                provider_cost_minor=product.monthly_price_minor,
                                provider_cost_currency=product.currency,
                                billing_parameters={
                                    "contract_term": self._provider._contract_term,
                                    "billing_cycle": self._provider._billing_cycle,
                                    "monthly_price_source": "contractTerms",
                                },
                                provider_available=False,
                            ),
                        )
                    except Exception as exc:
                        errors.append(f"upsert {product.id}/{location.id}: {exc}")
                    continue
                try:
                    await offers_repo.upsert_from_provider(
                        provider_key=PROVIDER_KEY,
                        product_id=product.id,
                        location_id=location.id,
                        update=OfferSpecUpdate(
                            name=offer_product.name,
                            vcpu=offer_product.vcpu,
                            ram_gb=offer_product.ram_gb,
                            disk_gb=offer_product.disk_gb,
                            traffic=offer_product.traffic,
                            provider_cost_minor=offer_product.monthly_price_minor,
                            provider_cost_currency=offer_product.currency,
                            billing_parameters={
                                "contract_term": self._provider._contract_term,
                                "billing_cycle": self._provider._billing_cycle,
                                "monthly_price_minor": offer_product.monthly_price_minor,
                                "available_locations": sorted(detail.available_locations),
                            },
                            provider_available=True,
                        ),
                    )
                except Exception as exc:
                    errors.append(f"upsert {product.id}/{location.id}: {exc}")
                    continue
                upserted += 1
                skipped += 0
        try:
            marked = await offers_repo.mark_unavailable(PROVIDER_KEY, available)
        except Exception as exc:
            errors.append(f"mark_unavailable: {exc}")
        else:
            if marked:
                logger.info("leaseweb ordering sync marked %d products unavailable", marked)
        return SyncResult(fetched, upserted, skipped, errors)

    async def sync_all(self) -> dict[str, SyncResult]:
        locations = await self.sync_locations()
        products = await self.sync_products()
        return {"locations": locations, "products": products}


# Back-compat alias (same convention as the other Leaseweb syncers).
LeasewebOrderingCatalogSyncer = LeaseWebOrderingCatalogSyncer
