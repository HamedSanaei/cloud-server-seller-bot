"""Provider auto-sync source adapters (STOREFRONT-V2).

The adapters translate each provider's native sync outcome into the
provider-neutral coordinator report: usable runs, partial runs, total
outages and hard failures — and a raising syncer never escapes as an
exception (one provider must never break another's run).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from cloud_platform.providers.hetzner.auto_sync import HetznerCatalogSyncSource
from cloud_platform.providers.hetzner.sync import LocationOfferReport, OfferSyncResult, SyncResult
from cloud_platform.providers.leaseweb.auto_sync import LeasewebCatalogSyncSource
from cloud_platform.providers.leaseweb.ordering_sync import SyncResult as LeasewebSyncResult


def _leaseweb_products(**overrides: Any) -> LeasewebSyncResult:
    values: dict[str, Any] = dict(
        total_fetched=6,
        total_upserted=6,
        total_skipped=0,
        errors=[],
        warnings=[],
        persistence_failures=[],
        availability_reconciled=True,
        marked_unavailable=1,
        offers_persisted=6,
        verified=frozenset({("VPS02_1", "FRA-01")}),
    )
    values.update(overrides)
    return LeasewebSyncResult(**values)


def _leaseweb_syncer(products: LeasewebSyncResult | Exception) -> AsyncMock:
    syncer = AsyncMock()
    if isinstance(products, Exception):
        syncer.sync_all = AsyncMock(side_effect=products)
    else:
        locations = LeasewebSyncResult(2, 2, 0, [])
        syncer.sync_all = AsyncMock(return_value={"locations": locations, "products": products})
    return syncer


class TestLeasewebSource:
    async def test_successful_run_maps_counters(self) -> None:
        source = LeasewebCatalogSyncSource(_leaseweb_syncer(_leaseweb_products()))
        report = await source.sync_catalog()
        assert report.provider_key == "leaseweb"
        assert report.ok is True
        assert report.complete is True
        assert report.discovered == 6
        assert report.persisted == 6
        assert report.retired == 1
        assert report.verified == frozenset({("VPS02_1", "FRA-01")})
        assert report.errors == ()

    async def test_total_auth_failure_is_not_usable(self) -> None:
        products = _leaseweb_products(
            total_fetched=0,
            total_upserted=0,
            errors=["authentication failed: no credential account could be probed"],
            availability_reconciled=False,
            verified=frozenset(),
        )
        report = await LeasewebCatalogSyncSource(_leaseweb_syncer(products)).sync_catalog()
        assert report.ok is False
        assert report.complete is False
        assert report.verified == frozenset()

    async def test_empty_but_clean_run_is_usable(self) -> None:
        products = _leaseweb_products(total_fetched=0, total_upserted=0)
        report = await LeasewebCatalogSyncSource(_leaseweb_syncer(products)).sync_catalog()
        assert report.ok is True

    async def test_persistence_failure_blocks_use(self) -> None:
        products = _leaseweb_products(persistence_failures=["upsert VPS02_1/FRA-01: boom"])
        report = await LeasewebCatalogSyncSource(_leaseweb_syncer(products)).sync_catalog()
        assert report.ok is False
        assert report.persistence_failures == ("upsert VPS02_1/FRA-01: boom",)

    async def test_raising_syncer_becomes_a_failed_report(self) -> None:
        report = await LeasewebCatalogSyncSource(
            _leaseweb_syncer(RuntimeError("transport down"))
        ).sync_catalog()
        assert report.ok is False
        assert report.complete is False
        assert report.errors


def _hetzner_syncer(
    *,
    locations: SyncResult | Exception | None = None,
    offers: OfferSyncResult | Exception | None = None,
) -> AsyncMock:
    syncer = AsyncMock()
    if isinstance(locations, Exception):
        syncer.sync_locations = AsyncMock(side_effect=locations)
    else:
        syncer.sync_locations = AsyncMock(
            return_value=locations if locations is not None else SyncResult(2, 2, 0, [])
        )
    if isinstance(offers, Exception):
        syncer.sync_offers = AsyncMock(side_effect=offers)
    else:
        syncer.sync_offers = AsyncMock(
            return_value=offers
            if offers is not None
            else OfferSyncResult(
                locations=(LocationOfferReport("fsn1", 2),),
                offers_written=2,
                marked_unavailable=0,
                warnings=(),
                verified=frozenset({("cx22", "fsn1")}),
            )
        )
    return syncer


class TestHetznerSource:
    async def test_successful_run_maps_counters(self) -> None:
        report = await HetznerCatalogSyncSource(_hetzner_syncer()).sync_catalog()
        assert report.provider_key == "hetzner"
        assert report.ok is True
        assert report.complete is True
        assert report.discovered == 2
        assert report.persisted == 2
        assert report.verified == frozenset({("cx22", "fsn1")})

    async def test_location_sync_failure_fails_the_run(self) -> None:
        report = await HetznerCatalogSyncSource(
            _hetzner_syncer(locations=RuntimeError("auth"))
        ).sync_catalog()
        assert report.ok is False
        assert report.complete is False
        assert report.errors

    async def test_offer_sync_failure_keeps_location_warnings(self) -> None:
        locations = SyncResult(2, 2, 0, ["page 2: ProviderUnavailable"])
        report = await HetznerCatalogSyncSource(
            _hetzner_syncer(locations=locations, offers=RuntimeError("down"))
        ).sync_catalog()
        assert report.ok is False
        assert any("page 2" in warning for warning in report.warnings)

    async def test_total_outage_is_not_usable(self) -> None:
        offers = OfferSyncResult(locations=(), offers_written=0, marked_unavailable=0, warnings=())
        report = await HetznerCatalogSyncSource(_hetzner_syncer(offers=offers)).sync_catalog()
        assert report.ok is False
        assert report.errors

    async def test_partial_location_errors_stay_usable(self) -> None:
        offers = OfferSyncResult(
            locations=(
                LocationOfferReport("fsn1", 2),
                LocationOfferReport("nbg1", 0, error="ProviderUnavailable"),
            ),
            offers_written=2,
            marked_unavailable=0,
            warnings=("nbg1: ProviderUnavailable",),
            verified=frozenset({("cx22", "fsn1")}),
        )
        report = await HetznerCatalogSyncSource(_hetzner_syncer(offers=offers)).sync_catalog()
        assert report.ok is True
        assert report.complete is False
        assert report.verified == frozenset({("cx22", "fsn1")})
