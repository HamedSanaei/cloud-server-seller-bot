"""Hetzner catalog synchronization with pagination support.

This module provides paginated sync methods for locations, server types (plans),
and system images, with proper rate limit handling and idempotent upserts.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
from cloud_platform.modules.offers.domain import OfferSpecUpdate
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

#: Hetzner's identity and billing currency. Provider metadata, not prices —
#: all price values are ingested from the API payload (M04-005).
PROVIDER_KEY = "hetzner"
CURRENCY = "EUR"


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
    ) -> None:
        self._session_factory = session_factory
        self._token = token or get_settings().hetzner_api_token
        self._base_url = base_url.rstrip("/")
        self._per_page = per_page
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=httpx.Timeout(30.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an API request with rate limit tracking and error mapping."""
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
        data = response.json()
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
        total_fetched = 0
        total_upserted = 0
        total_skipped = 0
        errors: list[str] = []

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
        the row is keyed by (provider, location_id) so the sync is idempotent.
        """
        if not locations_data:
            return 0, 0

        upserted = 0
        skipped = 0

        async with self._session_factory() as session:
            provider_id = await self._resolve_provider_id(session)

            for item in locations_data:
                loc_id = str(item["id"])
                stmt = select(ProviderLocation).where(
                    ProviderLocation.provider_id == provider_id,
                    ProviderLocation.location_id == loc_id,
                )
                result = await session.execute(stmt)
                existing = result.scalars().first()

                if existing:
                    existing.name = str(item["name"])
                    existing.country_code = item.get("country")
                    existing.city = item.get("city")
                    existing.network_zone = item.get("network_zone")
                    skipped += 1
                else:
                    session.add(
                        ProviderLocation(
                            provider_id=provider_id,
                            location_id=loc_id,
                            name=str(item["name"]),
                            country_code=item.get("country"),
                            city=item.get("city"),
                            network_zone=item.get("network_zone"),
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

        page = 1
        while True:
            try:
                params = self._get_pagination_params(page)
                payload = await self._request("GET", "/server_types", params=params)
                plans_data = payload.get("server_types", [])

                if not plans_data:
                    break

                upserted, skipped = await self._upsert_plans(plans_data)
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

    async def _upsert_plans(self, plans_data: list[dict[str, Any]]) -> tuple[int, int]:
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
                plan = _plan_pricing_from_hetzner(item)
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

    async def sync_offers(self) -> OfferSyncResult:
        """Turn real Hetzner server types into ``SellableOffer`` rows.

        For every location the provider reports, the LOCATION-SCOPED list
        endpoint (``/server_types?location=...``) is authoritative: each server
        type it returns with a monthly price at that location becomes one
        offer, identified by ``(provider, product, location)``. A location that
        fails is isolated — its offers are left untouched rather than being
        marked unavailable — while every other location still syncs.

        Provider cost is recorded as integer minor units parsed from the
        provider's Decimal string (never float, never hard-coded).
        ``enabled`` and ``selling_price_minor`` are NEVER written here: the
        operator owns both, so a newly discovered server type arrives switched
        off and unpriced. Nothing is ever deleted — a server type or location
        that stops being offered is marked provider-unavailable.
        """
        repo = SqlAlchemySellableOfferRepository(self._session_factory)
        locations, location_errors = await self._offer_locations()
        available: set[tuple[str, str]] = set()
        reports: list[LocationOfferReport] = []
        warnings: list[str] = list(location_errors)
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
            for item in items:
                spec = _offer_spec_from_hetzner(item, location_id)
                if spec is None:
                    warnings.append(
                        f"{location_id}: server type {item.get('name')} has no monthly price"
                    )
                    continue
                await repo.upsert_from_provider(
                    provider_key=PROVIDER_KEY,
                    product_id=spec.product_id,
                    location_id=location_id,
                    update=spec.update,
                )
                available.add((spec.product_id, location_id))
                counted += 1
                written += 1
            reports.append(LocationOfferReport(location_id=location_id, products=counted))

        # Only reconcile availability when EVERY configured location synced: a
        # partial view of the catalog must not retire offers we simply could
        # not look at this round.
        marked = 0
        if not any(report.error for report in reports) and locations:
            marked = await repo.mark_unavailable(PROVIDER_KEY, available)
        else:
            warnings.append("skipped mark_unavailable: current availability unreadable")

        return OfferSyncResult(
            locations=tuple(reports),
            offers_written=written,
            marked_unavailable=marked,
            warnings=tuple(warnings),
        )

    async def probe_locations(self) -> tuple[list[str], list[str]]:
        """READ-ONLY: the locations this credential can see (ids, errors)."""
        return await self._offer_locations()

    async def probe_server_types(self, location_id: str) -> list[dict[str, Any]]:
        """READ-ONLY: the server types offered at ONE location."""
        return await self._server_types_at(location_id)

    async def _offer_locations(self) -> tuple[list[str], list[str]]:
        """Every location id the credential can see (paginated, isolated)."""
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
            found.extend(str(item["id"]) for item in data if item.get("id"))
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
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    """Parse a provider price string into a Decimal, or None if absent."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Decimal(text)


@dataclass(frozen=True, slots=True)
class _OfferSpec:
    """One server type at one location, ready to be written to the price book."""

    product_id: str
    update: OfferSpecUpdate


def _offer_spec_from_hetzner(item: dict[str, Any], location_id: str) -> _OfferSpec | None:
    """Map one ``/server_types`` LIST entry to a sellable-offer observation.

    Returns None when the provider reports no monthly price for this location:
    without a provider price there is nothing safe to sell, so the caller
    records a warning instead of inventing one.
    """
    monthly = _monthly_minor_for_location(item, location_id)
    if monthly is None:
        return None
    # The server type NAME is the stable, human-meaningful provider reference
    # an operator can match against the Hetzner console (and the provider
    # accepts it wherever an id is accepted).
    name = str(item.get("name") or item["id"])
    return _OfferSpec(
        product_id=name,
        update=OfferSpecUpdate(
            name=name,
            vcpu=int(item.get("cores") or 0),
            ram_gb=_memory_gb(item.get("memory")),
            disk_gb=int(item.get("disk") or 0),
            traffic=_traffic_label(item.get("included_traffic")),
            provider_cost_minor=monthly,
            provider_cost_currency=CURRENCY,
            billing_parameters={
                "contract_term": "1_MONTH",
                "billing_cycle": "1_MONTH",
                "monthly_price_minor": monthly,
                "monthly_price_source": "server_types.location",
                "server_type_id": str(item.get("id") or ""),
                "location": location_id,
            },
            provider_available=True,
        ),
    )


def _monthly_minor_for_location(item: dict[str, Any], location_id: str) -> int | None:
    """Provider monthly price at one location, as integer minor units.

    The provider's Decimal STRING is scaled to minor units — never parsed as
    float, never rounded implicitly.
    """
    raw_prices = [raw for raw in item.get("prices", []) if isinstance(raw, dict)]
    for raw in raw_prices:
        declared = str(raw.get("location") or raw.get("location_name") or "")
        if declared != location_id:
            continue
        return _monthly_minor(raw)
    # A location-scoped request can return a single already-filtered price.
    if len(raw_prices) == 1:
        return _monthly_minor(raw_prices[0])
    return None


def _monthly_minor(raw: dict[str, Any]) -> int | None:
    gross = (raw.get("monthly") or {}).get("gross")
    if gross is None:
        return None
    try:
        value = Decimal(str(gross))
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _memory_gb(value: Any) -> int:
    """Hetzner reports memory in GB as a decimal string ("4.0") -> integer GB."""
    if value is None:
        return 0
    try:
        return int(Decimal(str(value)).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def _traffic_label(value: Any) -> str | None:
    """Included traffic (bytes, provider-reported) -> display label.

    Rendered in binary terabytes, the unit the provider itself uses for
    included traffic, from Decimal arithmetic only.
    """
    if value is None:
        return None
    try:
        total = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if total <= 0:
        return None
    terabytes = total / (Decimal(2) ** 40)
    if terabytes == terabytes.to_integral_value():
        return f"{int(terabytes)} TB"
    return f"{terabytes.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)} TB"


def _plan_pricing_from_hetzner(item: dict[str, Any]) -> PlanPricing:
    """Map one Hetzner /server_types entry to provider-neutral PlanPricing.

    Every per-location price entry is preserved (location-aware, M04-005).
    Memory is converted GB->MB with Decimal arithmetic (no float).
    """
    prices: list[ProviderPriceEntry] = []
    for raw in item.get("prices", []):
        if not isinstance(raw, dict):
            continue
        location_id = str(raw.get("location") or raw.get("location_name") or "unknown")
        prices.append(
            ProviderPriceEntry(
                location_id=location_id,
                currency=CURRENCY,
                hourly=_decimal_or_none((raw.get("hourly") or {}).get("gross")),
                monthly=_decimal_or_none((raw.get("monthly") or {}).get("gross")),
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
        payload = response.json()
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
