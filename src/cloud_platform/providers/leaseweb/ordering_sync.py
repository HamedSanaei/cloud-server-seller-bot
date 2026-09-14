"""Leaseweb ordering-VPS catalog sync (LEASEWEB-MVP).

Dynamic eligibility discovery replaces the old "configured locations are
sellable locations" model::

    discovered candidates (seeds + persisted + provider responses)
        -> read-only eligibility probe per location
        -> currently eligible locations -> current products
        -> enabled + operator-priced offers -> Telegram storefront

Configured locations (``LEASEWEB_LOCATIONS``) are DISCOVERY SEEDS ONLY:
worth probing, never an authorization allowlist. The source of truth for
account eligibility is always the live Leaseweb response.

Availability discipline (transient safety):

- only DEFINITIVE provider answers change what is sold: a 200 catalog
  (presence by listing, absence by empty listing) and an account
  403 denial hide/show decisively;
- transient failures (429, 5xx, timeouts, transport errors) preserve the
  last-known provider availability while being recorded as sync errors —
  a temporary Leaseweb problem never empties the storefront;
- an authentication (401) failure aborts the run without touching any
  availability flag.

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
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAuthenticationError,
    LeasewebNotFoundError,
)
from cloud_platform.providers.leaseweb.ordering import (
    KNOWN_VPS_DATACENTERS,
    LeaseWebOrderingProvider,
    LocationEligibility,
    LocationProbe,
    merge_candidates,
)

logger = logging.getLogger(__name__)

PROVIDER_KEY = "leaseweb"
CURRENCY = "EUR"


def ordering_provider_from_settings(settings: Any) -> LeaseWebOrderingProvider:
    """Build the ordering adapter from settings (shared construction).

    Used by the periodic worker refresh, the manual ``leaseweb sync-offers``
    command and the application container so all three run the same
    discovery pipeline. Configured locations are discovery seeds only
    (possibly empty) — never the sellability authority.
    """
    return LeaseWebOrderingProvider(
        api_key=settings.leaseweb_api_key,
        base_url=settings.leaseweb_api_base_url,
        locations=tuple(
            part.strip() for part in (settings.leaseweb_locations or "").split(",") if part.strip()
        ),
        os_allowlist=tuple(
            part.strip()
            for part in (settings.leaseweb_os_allowlist or "").split(",")
            if part.strip()
        ),
        order_os_only_free=settings.leaseweb_order_os_only_free,
        contract_term=settings.leaseweb_contract_term,
        billing_cycle=settings.leaseweb_billing_cycle,
        timeout_seconds=settings.leaseweb_timeout_seconds,
    )


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
        # Locations with a DEFINITIVE outcome this run (a 200 catalog or an
        # account denial). Only their offers may be hidden; every other
        # currently-available row is preserved (transient failures and
        # unprobed locations keep last-known state).
        resolved: set[str] = set()

        from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

        loc_repo = SqlAlchemyLocationRepository(self._session_factory)
        try:
            persisted = [r.location_id for r in await loc_repo.list_for_provider(PROVIDER_KEY)]
        except Exception as exc:
            logger.warning("leaseweb persisted locations unreadable: %s", exc)
            persisted = []

        # Best-effort unscoped discovery (+ authentication liveness). Any
        # failure here only narrows the candidate set; per-location probes
        # below still decide everything.
        try:
            unscoped = await self._provider.list_products_unscoped()
        except LeasewebAuthenticationError as exc:
            return SyncResult(fetched, upserted, skipped, [f"authentication failed: {exc}"])
        except Exception as exc:
            logger.info("leaseweb unscoped catalog unavailable: %s", exc)
            unscoped = []
        unscoped_locations = [product.location for product in unscoped if product.location]

        # Current availability picture (for transient preservation).
        try:
            current_rows = await offers_repo.list_all()
            current_available: set[tuple[str, str]] | None = {
                (str(row.product_id), str(row.location_id))
                for row in current_rows
                if row.provider_key == PROVIDER_KEY and row.provider_available
            }
        except Exception as exc:
            errors.append(f"current offers unreadable, availability left untouched: {exc}")
            current_available = None

        queue = list(
            merge_candidates(
                tuple(self._provider.discovery_seeds),
                KNOWN_VPS_DATACENTERS,
                persisted,
                unscoped_locations,
            )
        )
        probed: dict[str, LocationProbe] = {}
        while queue:
            location = queue.pop(0)
            if location in probed:
                continue
            try:
                probe = await self._provider.probe_location(location)
            except LeasewebAuthenticationError as exc:
                return SyncResult(
                    fetched, upserted, skipped, [*errors, f"authentication failed: {exc}"]
                )
            except Exception as exc:
                logger.warning("leaseweb probe of %s failed inconclusively: %s", location, exc)
                errors.append(f"probe {location}: {exc}")
                probed[location] = LocationProbe(
                    location, LocationEligibility.TRANSIENT_UNKNOWN, (), (), "probe error"
                )
                continue
            probed[location] = probe
            if probe.eligibility is LocationEligibility.FATAL_AUTHENTICATION:
                return SyncResult(
                    fetched, upserted, skipped, [*errors, f"authentication failed: {probe.note}"]
                )
            # Persist the discovery itself (display metadata included), so a
            # newly seen location is probed again automatically next run.
            # Best effort: a location-row write failure must not stop products.
            try:
                described = self._provider.describe_location(location)
                await loc_repo.upsert(
                    LocationRecord(
                        provider_key=PROVIDER_KEY,
                        location_id=described.id,
                        name=described.name,
                        country_code=described.country_code or None,
                        city=described.city,
                    )
                )
            except Exception as exc:
                errors.append(f"location {location}: {exc}")
            for extra in probe.discovered_locations:
                if extra not in probed and extra not in queue:
                    queue.append(extra)
            if probe.eligibility is not LocationEligibility.ELIGIBLE_AVAILABLE:
                # Definitive absence (empty catalog) or denial (account
                # scope): the location's offers stay out of `available` and
                # are hidden below. Anything else preserves last-known state.
                if probe.eligibility in (
                    LocationEligibility.ELIGIBLE_EMPTY,
                    LocationEligibility.INELIGIBLE_ACCOUNT,
                ):
                    resolved.add(location)
                continue
            resolved.add(location)
            for product in probe.products:
                fetched += 1
                try:
                    detail = await self._provider.get_product(location, product.id)
                except LeasewebNotFoundError:
                    # Definitive: the product is gone at this location.
                    logger.info("leaseweb product %s vanished at %s", product.id, location)
                    continue
                except Exception as exc:
                    errors.append(f"detail {product.id}/{location}: {exc}")
                    if (
                        current_available is not None
                        and (product.id, location) in current_available
                    ):
                        available.add((product.id, location))
                    continue
                available.add((product.id, location))
                offer_product = detail.product
                if detail.available_locations and location not in detail.available_locations:
                    # Product no longer sold at this location: flag unavailable.
                    try:
                        await offers_repo.upsert_from_provider(
                            provider_key=PROVIDER_KEY,
                            product_id=product.id,
                            location_id=location,
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
                        errors.append(f"upsert {product.id}/{location}: {exc}")
                    for extra in detail.available_locations:
                        if extra not in probed and extra not in queue:
                            queue.append(extra)
                    continue
                try:
                    await offers_repo.upsert_from_provider(
                        provider_key=PROVIDER_KEY,
                        product_id=product.id,
                        location_id=location,
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
                    errors.append(f"upsert {product.id}/{location}: {exc}")
                    continue
                upserted += 1
                skipped += 0
                for extra in detail.available_locations:
                    if extra not in probed and extra not in queue:
                        queue.append(extra)
        if current_available is None:
            errors.append("skipped mark_unavailable: current availability unreadable")
        else:
            for pair in current_available:
                if pair[1] not in resolved:
                    available.add(pair)
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
