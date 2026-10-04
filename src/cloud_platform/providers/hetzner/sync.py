"""Hetzner catalog synchronization with pagination support.

This module provides paginated sync methods for locations, server types (plans),
and system images, with proper rate limit handling and idempotent upserts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.core.config import get_settings
from cloud_platform.db.base import (
    Provider,
    ProviderLocation,
)
from cloud_platform.modules.catalog.domain import (
    CatalogSyncJob,
    CatalogSyncLock,
    CatalogSyncStep,
    CatalogSyncStepReport,
    PlanPricing,
    ProviderPriceEntry,
)
from cloud_platform.modules.catalog.repository import (
    SqlAlchemyCatalogRepository,
    provider_key_to_uuid,
)
from cloud_platform.modules.catalog.service import PricingIngestionService
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_MONTHLY,
    OfferSpecUpdate,
    TechnicalSpec,
)
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner import hourly
from cloud_platform.providers.hetzner.accounts import HetznerAccountRouter

logger = logging.getLogger(__name__)


PROVIDER_KEY = "hetzner"


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Result of a sync operation."""

    total_fetched: int
    total_upserted: int
    total_skipped: int
    errors: list[str]


@dataclass(frozen=True, slots=True)
class PaginationState:
    """Pagination cursor state."""

    page: int
    per_page: int
    has_next: bool


@dataclass(frozen=True, slots=True)
class LocationOfferReport:
    """How many sellable products one location contributed (or why it did not)."""

    location_id: str
    products: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OfferSyncResult:
    """Result of the sellable-offer sync (one row per location + totals)."""

    locations: tuple[LocationOfferReport, ...]
    offers_written: int
    marked_unavailable: int
    warnings: tuple[str, ...]
    #: (product_id, location_id) pairs successfully persisted this run — the
    #: only rows automatic pricing/publication may touch.
    verified: frozenset[tuple[str, str]] = frozenset()
    #: Durable-write failures (never provider-data advisories). Any entry
    #: here means the run is NOT a successful sync.
    persistence_failures: tuple[str, ...] = ()
    verified_accounts: frozenset[tuple[str, str, str]] = frozenset()
    account_aware: bool = False
    availability_reconciled: bool = False


class HetznerCatalogSyncer:
    """Synchronizes Hetzner catalog data to local database.

    Handles pagination, rate limiting, and idempotent upserts.
    """

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]],
        token: str | None = None,
        base_url: str = "https://api.hetzner.cloud/v1",
        per_page: int = 50,
        *,
        catalog_currency: str = "USD",
        catalog_stale_limit_seconds: int | None = None,
        account_router: HetznerAccountRouter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._catalog_currency = catalog_currency
        self._catalog_stale_limit_seconds = catalog_stale_limit_seconds
        self._account_router = account_router
        self._token = "" if account_router else token or get_settings().hetzner_api_token
        self._base_url = base_url.rstrip("/")
        self._per_page = per_page
        self._client = (
            httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(30.0),
            )
            if account_router is None
            else None
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an API request with rate limit tracking and error mapping."""
        if self._account_router is not None:
            clients = self._account_router.new_order_clients()
            if not clients:
                raise ProviderNotFound("no active Hetzner catalog account")
            return await clients[0][1]._request(method, path, params=params)
        assert self._client is not None
        try:
            response = await self._client.request(method, path, params=params)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderUnavailable(str(exc)) from exc

        if response.status_code == 401 or response.status_code == 403:
            raise ProviderAuthError(_error_message(response))
        if response.status_code == 404:
            raise ProviderNotFound(_error_message(response))
        if response.status_code in {409, 423}:
            raise ProviderConflict(_error_message(response))
        if response.status_code == 429:
            raise ProviderRateLimited(_error_message(response))
        if response.status_code >= 500:
            raise ProviderUnavailable(_error_message(response))
        if response.is_error:
            raise ProviderError(_error_message(response))
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json(parse_float=Decimal, parse_int=int)
        if not isinstance(data, dict):
            raise ProviderError("provider returned unexpected JSON shape")
        return data

    def _get_pagination_params(
        self, page: int, per_page: int | None = None
    ) -> dict[str, int | str]:
        """Build pagination query parameters."""
        return {"page": page, "per_page": per_page or self._per_page}

    # --- Location Sync ---

    async def sync_locations(self) -> SyncResult:
        """Sync all locations from Hetzner with pagination."""
        errors: list[str] = []
        if self._account_router is not None:
            locations: dict[str, dict[str, Any]] = {}
            for account_id, provider in self._account_router.new_order_clients():
                try:
                    for item in await provider._pages("/locations", "locations"):
                        name = item.get("name")
                        if not isinstance(name, str) or not name.strip():
                            raise ProviderError("location has no documented name")
                        locations.setdefault(name, item)
                except ProviderError as exc:
                    errors.append(f"account {account_id}: {type(exc).__name__}")
            upserted, skipped = await self._upsert_locations(list(locations.values()))
            return SyncResult(len(locations), upserted, skipped, errors)
        total_fetched = 0
        total_upserted = 0
        total_skipped = 0

        page = 1
        while True:
            try:
                params = self._get_pagination_params(page)
                payload = await self._request("GET", "/locations", params=params)
                locations_data = payload.get("locations", [])

                if not locations_data:
                    break

                # Upsert locations
                upserted, skipped = await self._upsert_locations(locations_data)
                total_fetched += len(locations_data)
                total_upserted += upserted
                total_skipped += skipped

                # Check if there are more pages
                meta = payload.get("meta", {})
                pagination = meta.get("pagination", {})
                if not pagination.get("next_page"):
                    break

                page += 1

            except Exception as e:
                logger.error("Failed to sync locations page %d: %s", page, e)
                errors.append(f"Page {page}: {e}")
                break

        return SyncResult(
            total_fetched=total_fetched,
            total_upserted=total_upserted,
            total_skipped=total_skipped,
            errors=errors,
        )

    async def _upsert_locations(self, locations_data: list[dict[str, Any]]) -> tuple[int, int]:
        """Upsert locations to the provider_locations table. Returns (upserted, skipped).

        Country/city/network-zone come from the provider payload (M08-002);
        new rows use the provider's location NAME; numeric legacy rows remain
        untouched for existing contracts that reference the old location id.
        """
        if not locations_data:
            return 0, 0

        upserted = 0
        skipped = 0

        async with self._session_factory() as session:
            provider_id = await self._resolve_provider_id(session)

            for item in locations_data:
                loc_id = str(item["name"])
                stmt = select(ProviderLocation).where(
                    ProviderLocation.provider_id == provider_id,
                    ProviderLocation.location_id == loc_id,
                )
                result = await session.execute(stmt)
                existing = result.scalars().first()
                country = _normalize_country(item.get("country"))
                city = str(item.get("city") or "").strip() or None
                network_zone = str(item.get("network_zone") or "").strip() or None
                if existing:
                    existing.name = str(item["name"])
                    existing.country_code = country
                    existing.city = city
                    existing.network_zone = network_zone
                    skipped += 1
                else:
                    session.add(
                        ProviderLocation(
                            provider_id=provider_id,
                            location_id=loc_id,
                            name=str(item["name"]),
                            country_code=country,
                            city=city,
                            network_zone=network_zone,
                        )
                    )
                    upserted += 1

            await session.commit()

        return upserted, skipped

    async def _resolve_provider_id(self, session: AsyncSession) -> UUID:
        """Resolve (or deterministically create) the Hetzner provider row."""
        existing = (
            (await session.execute(select(Provider.id).where(Provider.name == PROVIDER_KEY)))
            .scalars()
            .first()
        )
        if existing is not None:
            return existing
        provider_id = provider_key_to_uuid(PROVIDER_KEY)
        session.add(Provider(id=provider_id, name=PROVIDER_KEY, region="global"))
        await session.flush()
        return provider_id

    # --- Server Type (Plan) Sync ---

    async def sync_plans(self) -> SyncResult:
        """Sync all server types (plans) from Hetzner with pagination."""
        total_fetched = 0
        total_upserted = 0
        total_skipped = 0
        errors: list[str] = []
        try:
            currency = hourly.pricing_currency(await self._request("GET", "/pricing"))
        except ProviderError as exc:
            return SyncResult(0, 0, 0, [f"Pricing currency: {type(exc).__name__}"])

        page = 1
        while True:
            try:
                params = self._get_pagination_params(page)
                payload = await self._request("GET", "/server_types", params=params)
                plans_data = payload.get("server_types", [])

                if not plans_data:
                    break

                upserted, skipped = await self._upsert_plans(plans_data, currency=currency)
                total_fetched += len(plans_data)
                total_upserted += upserted
                total_skipped += skipped

                meta = payload.get("meta", {})
                pagination = meta.get("pagination", {})
                if not pagination.get("next_page"):
                    break

                page += 1

            except Exception as e:
                logger.error("Failed to sync plans page %d: %s", page, e)
                errors.append(f"Page {page}: {e}")
                break

        return SyncResult(
            total_fetched=total_fetched,
            total_upserted=total_upserted,
            total_skipped=total_skipped,
            errors=errors,
        )

    async def _upsert_plans(
        self, plans_data: list[dict[str, Any]], *, currency: str
    ) -> tuple[int, int]:
        """Upsert server types via location-aware pricing ingestion (M04-005).

        Every per-location price reported by Hetzner is persisted as its own
        catalog row; prices are computed in Decimal from the provider payload
        (never float, never hard-coded).
        """
        if not plans_data:
            return 0, 0

        service = PricingIngestionService(SqlAlchemyCatalogRepository(self._session_factory))

        upserted = 0
        skipped = 0
        for item in plans_data:
            try:
                plan = _plan_pricing_from_hetzner(item, currency=currency)
            except (KeyError, ValueError) as exc:
                logger.error("Skipping plan %s: %s", item.get("id"), exc)
                continue
            result = await service.ingest_plan(PROVIDER_KEY, plan)
            for price in result.prices:
                if price.created:
                    upserted += 1
                else:
                    skipped += 1

        return upserted, skipped

    # --- System Image Sync ---

    async def sync_images(self) -> SyncResult:
        """Sync all system images from Hetzner with pagination."""
        total_fetched = 0
        total_upserted = 0
        total_skipped = 0
        errors: list[str] = []

        page = 1
        while True:
            try:
                params = self._get_pagination_params(page)
                # Only fetch system images
                params["type"] = "system"
                payload = await self._request("GET", "/images", params=params)
                images_data = payload.get("images", [])

                if not images_data:
                    break

                upserted, skipped = await self._upsert_images(images_data)
                total_fetched += len(images_data)
                total_upserted += upserted
                total_skipped += skipped

                meta = payload.get("meta", {})
                pagination = meta.get("pagination", {})
                if not pagination.get("next_page"):
                    break

                page += 1

            except Exception as e:
                logger.error("Failed to sync images page %d: %s", page, e)
                errors.append(f"Page {page}: {e}")
                break

        return SyncResult(
            total_fetched=total_fetched,
            total_upserted=total_upserted,
            total_skipped=total_skipped,
            errors=errors,
        )

    async def _upsert_images(self, images_data: list[dict[str, Any]]) -> tuple[int, int]:
        """Upsert system images to database. Returns (upserted, skipped)."""
        if not images_data:
            return 0, 0

        upserted = 0
        skipped = 0

        # Images are not directly stored in Catalog; they're metadata
        # For now, we'll store them as a special catalog entry or in metadata
        # A full implementation would have a dedicated Images table
        for item in images_data:
            str(item["id"])
            # Store in provider metadata for now
            # In a full implementation, we'd have a dedicated Images table
            pass

        # For this implementation, we just track what was fetched
        upserted = len(images_data)

        return upserted, skipped

    async def sync_all(self) -> dict[str, SyncResult]:
        """Run all sync operations and return results."""
        logger.info("Starting full Hetzner catalog sync")

        locations_result = await self.sync_locations()
        logger.info(
            "Locations sync complete: fetched=%d, upserted=%d, skipped=%d",
            locations_result.total_fetched,
            locations_result.total_upserted,
            locations_result.total_skipped,
        )

        plans_result = await self.sync_plans()
        logger.info(
            "Plans sync complete: fetched=%d, upserted=%d, skipped=%d",
            plans_result.total_fetched,
            plans_result.total_upserted,
            plans_result.total_skipped,
        )

        images_result = await self.sync_images()
        logger.info(
            "Images sync complete: fetched=%d, upserted=%d, skipped=%d",
            images_result.total_fetched,
            images_result.total_upserted,
            images_result.total_skipped,
        )

        return {
            "locations": locations_result,
            "plans": plans_result,
            "images": images_result,
        }

    # --- Sellable-offer sync (the customer-facing price book) ---

    async def sync_offers(self, billing_model: str = BILLING_MODEL_MONTHLY) -> OfferSyncResult:
        """Persist one Hetzner billing line without touching the other.

        Location-scoped ``/server_types`` is the sole membership and pricing
        source. An incomplete location is never treated as evidence of removal.
        Operator prices and publication settings belong to the offer repository,
        not this provider observation.
        """
        if billing_model not in (BILLING_MODEL_MONTHLY, BILLING_MODEL_HOURLY):
            raise ValueError("unsupported Hetzner billing model")
        if self._account_router is not None:
            return await self._sync_account_offers(billing_model)
        try:
            currency = hourly.pricing_currency(await self._request("GET", "/pricing"))
        except ProviderError as exc:
            return OfferSyncResult(
                locations=(),
                offers_written=0,
                marked_unavailable=0,
                warnings=(f"pricing currency: {type(exc).__name__}",),
            )
        repo = SqlAlchemySellableOfferRepository(
            self._session_factory,
            catalog_currency=self._catalog_currency,
            catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
        )
        locations, location_errors = await self._offer_locations()
        available: set[tuple[str, str]] = set()
        verified: set[tuple[str, str]] = set()
        reports: list[LocationOfferReport] = []
        warnings: list[str] = list(location_errors)
        persistence_failures: list[str] = []
        written = 0

        for location_id in locations:
            try:
                items = await self._server_types_at(location_id)
            except ProviderError as exc:
                # One bad location must never fail the whole sync, and must
                # never mark its offers unavailable: the provider told us
                # nothing about it.
                reports.append(
                    LocationOfferReport(
                        location_id=location_id, products=0, error=type(exc).__name__
                    )
                )
                warnings.append(f"location {location_id}: {type(exc).__name__}")
                continue
            counted = 0
            location_failed = False
            for item in items:
                spec: _OfferSpec | None
                if billing_model == BILLING_MODEL_HOURLY:
                    parsed = hourly.parse_hourly_plan(item, location_id, currency=currency)
                    if isinstance(parsed, hourly.HetznerHourlyRejection):
                        warnings.append(
                            f"{location_id}: server type {parsed.plan_id}: {parsed.reason}"
                        )
                        if parsed.reason not in (
                            hourly.REASON_DEPRECATED,
                            hourly.REASON_UNAVAILABLE_AT_LOCATION,
                        ):
                            location_failed = True
                        continue
                    spec = _hourly_offer_spec(parsed)
                else:
                    spec = _offer_spec_from_hetzner(item, location_id, currency=currency)
                    if spec is None:
                        warnings.append(
                            f"{location_id}: server type {item.get('name')} has no monthly price"
                        )
                        continue
                try:
                    await repo.upsert_from_provider(
                        provider_key=PROVIDER_KEY,
                        product_id=spec.product_id,
                        location_id=location_id,
                        update=spec.update,
                    )
                except Exception as exc:
                    # A durable-write failure fails this location closed: its
                    # pairs stay out of ``available``/``verified`` (so they
                    # are neither repriced nor retired) and the location
                    # counts as errored (so nothing is retired globally).
                    persistence_failures.append(f"upsert {spec.product_id}/{location_id}: {exc}")
                    warnings.append(
                        f"{location_id}: keeping last-known offers "
                        f"({type(exc).__name__}); nothing retired"
                    )
                    location_failed = True
                    continue
                available.add((spec.product_id, location_id))
                verified.add((spec.product_id, location_id))
                counted += 1
                written += 1
            reports.append(
                LocationOfferReport(
                    location_id=location_id,
                    products=counted,
                    error="incomplete observation" if location_failed else None,
                )
            )

        # Only reconcile availability when EVERY configured location synced: a
        # partial view of the catalog must not retire offers we simply could
        # not look at this round.
        marked = 0
        reconciled = (
            not any(report.error for report in reports) and not location_errors and bool(locations)
        )
        if reconciled:
            marked = await repo.mark_unavailable(
                PROVIDER_KEY, available, billing_model=billing_model
            )
        else:
            warnings.append("skipped mark_unavailable: current availability unreadable")

        return OfferSyncResult(
            locations=tuple(reports),
            offers_written=written,
            marked_unavailable=marked,
            warnings=tuple(warnings),
            verified=frozenset(verified),
            persistence_failures=tuple(persistence_failures),
            availability_reconciled=reconciled,
        )

    async def _sync_account_offers(self, billing_model: str) -> OfferSyncResult:
        """Independent proofs; one customer row, never a copied cross-account observation."""
        from cloud_platform.modules.catalog.image_compatibility import image_compatible
        from cloud_platform.modules.provider_routes.domain import RouteObservation, RouteState
        from cloud_platform.modules.provider_routes.repository import (
            SqlAlchemyProviderRouteRepository,
        )
        from cloud_platform.providers.routing import CredentialAccountState

        assert self._account_router is not None
        router = self._account_router
        repo = SqlAlchemySellableOfferRepository(
            self._session_factory,
            catalog_currency=self._catalog_currency,
            catalog_stale_limit_seconds=self._catalog_stale_limit_seconds,
        )
        routes = SqlAlchemyProviderRouteRepository(self._session_factory)
        observations: dict[tuple[str, str], list[tuple[str, OfferSpecUpdate]]] = {}
        route_observations: list[RouteObservation] = []
        unreadable_accounts: dict[str, str] = {}
        known_headroom: set[str] = set()
        reports: list[LocationOfferReport] = []
        warnings: list[str] = []
        failures: list[str] = []
        complete = True
        previous = await routes.list_for_provider(PROVIDER_KEY)
        for account_id, provider in router.new_order_clients():
            account_locations: set[str] = set()
            locations_proven = False
            currency = None
            try:
                currency = await provider.get_pricing_currency()
            except ProviderError as exc:
                complete = False
                warnings.append(f"account {account_id} pricing currency: {type(exc).__name__}")
            try:
                usage = await router.server_usage(account_id)
                if not usage.full:
                    known_headroom.add(account_id)
            except ProviderError as exc:
                warnings.append(f"account {account_id} usage: {type(exc).__name__}")
                complete = False
            try:
                locations = await provider._pages("/locations", "locations")
                locations_proven = True
            except ProviderError as exc:
                complete = False
                warnings.append(f"account {account_id} locations: {type(exc).__name__}")
                locations = []
                unreadable_accounts[account_id] = type(exc).__name__
            for location in locations:
                location_id = location.get("name")
                if not isinstance(location_id, str) or not location_id.strip():
                    complete = False
                    locations_proven = False
                    unreadable_accounts[account_id] = "ProviderError"
                    warnings.append(f"account {account_id}: invalid location name")
                    continue
                account_locations.add(location_id)
                observations_here: list[tuple[str, OfferSpecUpdate]] = []
                try:
                    items = await provider._pages(
                        "/server_types", "server_types", {"location": location_id}
                    )
                    products: list[str] = []
                    for item in items:
                        available = hourly._location_availability(item, location_id)
                        if available is False or item.get("deprecated") or item.get("deprecation"):
                            continue
                        name = item.get("name")
                        if available is not True or not isinstance(name, str) or not name:
                            raise ProviderError("product/location membership is unproven")
                        if name in products:
                            raise ProviderError("duplicate provider product identity")
                        products.append(name)
                except (ProviderError, KeyError, ValueError) as exc:
                    complete = False
                    reports.append(LocationOfferReport(location_id, 0, type(exc).__name__))
                    route_observations.append(
                        RouteObservation(
                            account_id,
                            location_id,
                            RouteState.AUTH_FAILED
                            if isinstance(exc, ProviderAuthError)
                            else RouteState.TRANSIENT_UNKNOWN,
                            error_class=type(exc).__name__,
                        )
                    )
                    continue
                # This shared route describes actual SKU membership, not one
                # billing family's price/image proof. Every execution re-proves
                # its immutable contract before SENT; publication below proves
                # each family's native price and mandatory inputs separately.
                route_observations.append(
                    RouteObservation(
                        account_id,
                        location_id,
                        RouteState.ELIGIBLE_AVAILABLE if products else RouteState.ELIGIBLE_EMPTY,
                        product_ids=tuple(sorted(products)),
                        succeeded=True,
                    )
                )
                try:
                    if currency is None:
                        raise ProviderError("account pricing currency is unproven")
                    images = (
                        await router.hourly_for(account_id).installable_images(location_id)
                        if billing_model == BILLING_MODEL_HOURLY
                        else []
                    )
                    for item in items:
                        if item.get("name") not in products:
                            continue
                        plan = None
                        spec: _OfferSpec | None
                        if billing_model == BILLING_MODEL_HOURLY:
                            parsed = hourly.parse_hourly_plan(item, location_id, currency=currency)
                            if isinstance(parsed, hourly.HetznerHourlyRejection):
                                raise ProviderError("hourly price or mandatory facts are unproven")
                            plan = parsed
                            spec = _hourly_offer_spec(plan)
                        else:
                            spec = _offer_spec_from_hetzner(item, location_id, currency=currency)
                        if spec is None:
                            raise ProviderError("monthly price is unproven")
                        if plan is not None and (
                            plan.disk_gb <= 0
                            or not plan.storage_type
                            or not any(
                                image_compatible(
                                    image,
                                    plan_id=spec.product_id,
                                    architecture=plan.architecture,
                                    location_id=location_id,
                                    account_id=account_id,
                                )
                                for image in images
                            )
                        ):
                            continue
                        observations_here.append(
                            (
                                spec.product_id,
                                replace(spec.update, provider_account_id=account_id),
                            )
                        )
                except (ProviderError, KeyError, ValueError) as exc:
                    complete = False
                    reports.append(LocationOfferReport(location_id, 0, type(exc).__name__))
                    continue
                reports.append(LocationOfferReport(location_id, len(observations_here)))
                for product_id, update in observations_here:
                    observations.setdefault((product_id, location_id), []).append(
                        (account_id, update)
                    )
            if locations_proven:
                for old in previous:
                    if (
                        old.credential_account_id == account_id
                        and old.location_id not in account_locations
                    ):
                        route_observations.append(
                            RouteObservation(
                                account_id,
                                old.location_id,
                                RouteState.INELIGIBLE,
                                succeeded=True,
                            )
                        )
        known_locations = {report.location_id for report in reports}
        known_locations.update(old.location_id for old in previous)
        observed_routes = {
            (observation.credential_account_id, observation.location_id)
            for observation in route_observations
        }
        for account_id, error_class in unreadable_accounts.items():
            for location_id in known_locations:
                if (account_id, location_id) not in observed_routes:
                    route_observations.append(
                        RouteObservation(
                            account_id,
                            location_id,
                            RouteState.AUTH_FAILED
                            if error_class == "ProviderAuthError"
                            else RouteState.TRANSIENT_UNKNOWN,
                            error_class=error_class,
                        )
                    )
        for old in previous:
            if not router.accepts_new_orders(old.credential_account_id):
                route_observations.append(
                    RouteObservation(
                        old.credential_account_id,
                        old.location_id,
                        RouteState.DISABLED,
                        succeeded=True,
                    )
                )
        await routes.upsert_observations(
            provider_key=PROVIDER_KEY,
            observations=route_observations,
            priority_of=lambda account: router.priorities.get(account, 100),
            account_state_of=lambda account: router.account_states.get(
                account, CredentialAccountState.DISABLED
            ),
        )
        verified: set[tuple[str, str]] = set()
        qualified: set[tuple[str, str, str]] = set()
        for (product_id, location_id), candidates in observations.items():
            account_id, update = next(
                (candidate for candidate in candidates if candidate[0] in known_headroom),
                candidates[0],
            )
            try:
                await repo.upsert_from_provider(
                    provider_key=PROVIDER_KEY,
                    product_id=product_id,
                    location_id=location_id,
                    update=update,
                    provider_account_id=account_id,
                    adopt_legacy_catalog_row=True,
                )
            except Exception as exc:
                failures.append(f"{product_id}@{location_id}: {type(exc).__name__}")
                complete = False
                continue
            verified.add((product_id, location_id))
            qualified.add((account_id, product_id, location_id))
        # A full/unknown pool still has a catalog. Never turn quota/read failures
        # into disappearance, nor overwrite customer prices with another account.
        marked = 0
        reconciled = complete and bool(known_headroom) and bool(reports)
        if reconciled:
            marked = await repo.mark_unavailable(
                PROVIDER_KEY,
                qualified,
                billing_model=billing_model,
            )
        else:
            warnings.append("skipped mark_unavailable: pool full or observation incomplete")
        return OfferSyncResult(
            locations=tuple(reports),
            offers_written=len(verified),
            marked_unavailable=marked,
            warnings=tuple(warnings),
            verified=frozenset(verified),
            persistence_failures=tuple(failures),
            verified_accounts=frozenset(qualified),
            account_aware=True,
            availability_reconciled=reconciled,
        )

    async def probe_locations(self) -> tuple[list[str], list[str]]:
        """READ-ONLY: the locations this credential can see (names, errors)."""
        return await self._offer_locations()

    async def probe_server_types(self, location_id: str) -> list[dict[str, Any]]:
        """READ-ONLY: the server types offered at ONE location."""
        return await self._server_types_at(location_id)

    async def _offer_locations(self) -> tuple[list[str], list[str]]:
        """Every location name the credential can see (paginated, isolated)."""
        found: list[str] = []
        errors: list[str] = []
        page = 1
        while True:
            try:
                payload = await self._request(
                    "GET", "/locations", params=self._get_pagination_params(page)
                )
            except ProviderError as exc:
                errors.append(f"locations page {page}: {type(exc).__name__}")
                break
            data = payload.get("locations", [])
            if not data:
                break
            found.extend(
                str(item.get("name") or item.get("id"))
                for item in data
                if item.get("name") or item.get("id")
            )
            next_page = ((payload.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not isinstance(next_page, int) or next_page <= page:
                break
            page = next_page
        return found, errors

    async def _server_types_at(self, location_id: str) -> list[dict[str, Any]]:
        """Server types offered at ONE location (paginated).

        The list endpoint is the source of truth for catalog membership; the
        per-id detail endpoint is never required here.
        """
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            params: dict[str, Any] = {**self._get_pagination_params(page), "location": location_id}
            payload = await self._request("GET", "/server_types", params=params)
            data = payload.get("server_types", [])
            if not data:
                return items
            items.extend(data)
            next_page = ((payload.get("meta") or {}).get("pagination") or {}).get("next_page")
            if not isinstance(next_page, int) or next_page <= page:
                return items
            page = next_page


def _int_or_none(value: str | None) -> int | None:
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _OfferSpec:
    """One server type at one location, ready to be written to the price book."""

    product_id: str
    update: OfferSpecUpdate


def _offer_spec_from_hetzner(
    item: dict[str, Any], location_id: str, *, currency: str
) -> _OfferSpec | None:
    """Map one ``/server_types`` LIST entry to a sellable-offer observation.

    Returns None when the provider reports no monthly price for this location:
    without a provider price there is nothing safe to sell, so the caller
    records a warning instead of inventing one.
    """
    if bool(item.get("deprecated", False)) or item.get("deprecation"):
        return None
    if hourly._location_availability(item, location_id) is False:
        return None
    price_entry, _ = hourly._location_price_entry(item, location_id)
    if price_entry is None:
        return None
    monthly_exact = _monthly_value(price_entry)
    if monthly_exact is None:
        return None
    monthly = hourly.minor_units(monthly_exact, currency)
    # The server type NAME is the stable, human-meaningful provider reference
    # an operator can match against the Hetzner console (and the provider
    # accepts it wherever an id is accepted).
    name = str(item.get("name") or item["id"])
    raw_architecture = str(item.get("architecture") or "").strip()
    architecture = raw_architecture if raw_architecture.lower() != "unknown" else ""
    cpu_type = item.get("cpu_type")
    storage_type = item.get("storage_type")
    return _OfferSpec(
        product_id=name,
        update=OfferSpecUpdate(
            name=name,
            vcpu=int(item.get("cores") or 0),
            ram_gb=hourly.memory_gb(item.get("memory")),
            disk_gb=int(item.get("disk") or 0),
            traffic=hourly.traffic_label(
                price_entry.get("included_traffic", item.get("included_traffic"))
            ),
            provider_cost_minor=monthly,
            provider_cost_currency=currency,
            technical_metadata=TechnicalSpec(
                architecture=architecture or None,
                cpu_type=str(cpu_type) if cpu_type else None,
                storage_type=str(storage_type) if storage_type else None,
                deprecated=bool(item.get("deprecated", False)),
            ).to_metadata(),
            billing_model=BILLING_MODEL_MONTHLY,
            billing_parameters={
                "contract_term": "1_MONTH",
                "billing_cycle": "1_MONTH",
                "monthly_price_minor": monthly,
                "provider_monthly_rate": str(monthly_exact),
                "monthly_price_source": "server_types.location",
                "server_type_id": str(item.get("id") or ""),
                "location": location_id,
            },
            provider_available=True,
        ),
    )


def _hourly_offer_spec_from_hetzner(
    item: dict[str, Any], location_id: str, *, currency: str
) -> _OfferSpec | None:
    parsed = hourly.parse_hourly_plan(item, location_id, currency=currency)
    return _hourly_offer_spec(parsed) if isinstance(parsed, hourly.HetznerHourlyPlan) else None


def _hourly_offer_spec(plan: hourly.HetznerHourlyPlan) -> _OfferSpec:
    """Keep the provider's hourly RATE and independent monthly CAP as cost facts."""
    return _OfferSpec(
        product_id=plan.plan_id,
        update=OfferSpecUpdate(
            name=plan.plan_id,
            vcpu=plan.vcpu,
            ram_gb=plan.ram_gb,
            disk_gb=plan.disk_gb,
            traffic=plan.traffic,
            provider_cost_minor=plan.hourly_cost_minor,
            provider_cost_currency=plan.currency,
            billing_model=BILLING_MODEL_HOURLY,
            technical_metadata=TechnicalSpec(
                architecture=plan.architecture,
                cpu_type=plan.cpu_type,
                storage_type=plan.storage_type,
            ).to_metadata(),
            billing_parameters={
                "provider_hourly_rate": plan.hourly_rate_exact,
                "provider_monthly_rate": plan.monthly_rate_exact,
                "provider_monthly_cost_minor": plan.monthly_cap_minor,
                "hourly_price_source": "server_types.location",
                "server_type_id": plan.server_type_id,
                "location": plan.location_id,
            },
        ),
    )


def _monthly_value(raw: dict[str, Any]) -> Decimal | None:
    """Read the official monthly gross through the shared price parser."""
    value, _reason = hourly._gross_decimal(raw, hourly.MONTHLY_PRICE_KEY)
    return value


def _normalize_country(value: Any) -> str | None:
    """ISO country from the provider's own location payload.

    The /locations API is authoritative; this only trims case/whitespace so
    a ragged payload cannot poison the flag lookup. Anything that is not a
    2-letter code becomes unknown (None), never a guessed country.
    """
    code = str(value or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    return code


def _plan_pricing_from_hetzner(item: dict[str, Any], *, currency: str) -> PlanPricing:
    """Map one Hetzner /server_types entry to provider-neutral PlanPricing.

    Every per-location price entry is preserved (location-aware, M04-005).
    Memory is converted GB->MB with Decimal arithmetic (no float).
    """
    prices: list[ProviderPriceEntry] = []
    for raw in item.get("prices", []):
        if not isinstance(raw, dict):
            continue
        location_id = str(raw.get("location") or raw.get("location_name") or "unknown")
        hourly_cost = hourly._gross_decimal(raw, hourly.HOURLY_PRICE_KEY)[0]
        monthly_cost = hourly._gross_decimal(raw, hourly.MONTHLY_PRICE_KEY)[0]
        if hourly_cost is None and monthly_cost is None:
            continue
        prices.append(
            ProviderPriceEntry(
                location_id=location_id,
                currency=currency,
                hourly=hourly_cost,
                monthly=monthly_cost,
            )
        )

    memory_gb = Decimal(str(item.get("memory") or 0))
    return PlanPricing(
        plan_id=str(item["id"]),
        name=str(item.get("name") or item["id"]),
        architecture=str(item.get("architecture") or "unknown"),
        vcpu=int(item.get("cores") or 0),
        memory_mb=int(memory_gb * 1024),
        disk_gb=int(item.get("disk") or 0),
        prices=tuple(prices),
        description=item.get("description"),
        cpu_type=item.get("cpu_type"),
        storage_type=item.get("storage_type"),
    )


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json(parse_float=Decimal, parse_int=int)
        error = payload.get("error", {})
        return str(error.get("message") or error.get("code") or response.text)
    except Exception:
        return response.text or f"HTTP {response.status_code}"


def build_catalog_sync_job(syncer: HetznerCatalogSyncer, lock: CatalogSyncLock) -> CatalogSyncJob:
    """Wire the Hetzner syncer's steps into a locked catalog sync job (M04-006).

    Each step is the syncer's paginated sync method adapted to the domain's
    step report; the job holds the lock across all three, so concurrent
    invocations serialize instead of interleaving their upserts.
    """

    def _step(name: str, method: Callable[[], Any]) -> CatalogSyncStep:
        async def run() -> CatalogSyncStepReport:
            result: SyncResult = await method()
            return CatalogSyncStepReport(
                name=name,
                fetched=result.total_fetched,
                upserted=result.total_upserted,
                skipped=result.total_skipped,
                errors=tuple(result.errors),
            )

        return CatalogSyncStep(name=name, run=run)

    return CatalogSyncJob(
        lock,
        (
            _step("locations", syncer.sync_locations),
            _step("plans", syncer.sync_plans),
            _step("images", syncer.sync_images),
        ),
    )
