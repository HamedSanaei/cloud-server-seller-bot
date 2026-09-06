"""Tests for the LeaseWeb catalog/pricing mapper (Decimal-only, no float)."""

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.providers.leaseweb.sync import plan_pricing_from_leaseweb


def test_mapper_preserves_decimal_prices() -> None:
    pricing = plan_pricing_from_leaseweb(
        {
            "id": "lsw.mini",
            "name": "Mini",
            "cpu": 1,
            "memoryMb": 1024,
            "disk": 25,
            "pricePerHour": "0.015",
            "pricePerMonth": "10.00",
            "currency": "EUR",
        },
        location_id="AMS-01",
    )
    assert pricing.plan_id == "lsw.mini"
    assert pricing.prices[0].location_id == "AMS-01"
    assert pricing.prices[0].hourly == Decimal("0.015")
    assert pricing.prices[0].monthly == Decimal("10.00")
    assert pricing.vcpu == 1


def test_mapper_requires_price() -> None:
    with pytest.raises(ValueError, match="neither hourly nor monthly"):
        plan_pricing_from_leaseweb({"id": "x", "cpu": 1}, location_id="AMS-01")


def test_mapper_requires_location() -> None:
    with pytest.raises(ValueError, match="location_id"):
        plan_pricing_from_leaseweb({"id": "x", "pricePerHour": "0.01"}, location_id=" ")


def test_mapper_rejects_negative_price() -> None:
    with pytest.raises(ValueError, match="neither hourly nor monthly"):
        plan_pricing_from_leaseweb({"id": "x", "pricePerHour": "-1"}, location_id="AMS-01")


def test_mapper_missing_id_raises() -> None:
    with pytest.raises(ValueError, match="missing id"):
        plan_pricing_from_leaseweb({"pricePerHour": "0.01"}, location_id="AMS-01")


def test_decimal_parser_variants() -> None:
    from cloud_platform.providers.leaseweb.sync import _decimal_from_provider

    assert _decimal_from_provider(None) is None
    assert _decimal_from_provider(True) is None
    assert _decimal_from_provider(-5) is None
    assert _decimal_from_provider("junk") is None
    assert _decimal_from_provider(0.015) == Decimal("0.015")
    assert _decimal_from_provider("12.34") == Decimal("12.34")


def test_mapper_uses_resources_and_extras() -> None:
    pricing = plan_pricing_from_leaseweb(
        {
            "id": "lsw.mid",
            "name": "Mid",
            "architecture": "arm64",
            "cpu": 2,
            "memoryMb": 4096,
            "disk": 80,
            "pricePerHour": "0.03",
            "pricePerMonth": "21.00",
            "currency": "USD",
            "description": "d",
            "cpuType": "shared",
            "storageType": "ssd",
        },
        location_id="FRA-01",
    )
    assert pricing.architecture == "arm64"
    assert pricing.memory_mb == 4096
    assert pricing.disk_gb == 80
    assert pricing.prices[0].currency == "USD"
    assert pricing.cpu_type == "shared"
    assert pricing.storage_type == "ssd"


class FakeSyncSession:
    """Scripted session: execute() pops the next canned result."""

    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.added: list[object] = []
        self.committed = False
        self.flushed = False

    async def __aenter__(self) -> "FakeSyncSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, stmt: object) -> Any:
        return self._results.pop(0)

    def add(self, row: object) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True


def _scalars_result(value: object) -> Any:
    result = MagicMock()
    result.scalars = MagicMock(return_value=MagicMock(first=lambda: value))
    return result


def _provider() -> Any:
    provider = MagicMock()
    provider.list_locations = AsyncMock(return_value=[])
    provider.list_plans = AsyncMock(return_value=[])
    return provider


async def test_sync_locations_creates_provider_row_and_locations() -> None:
    from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer

    loc = MagicMock()
    loc.id = "AMS-01"
    loc.name = "Amsterdam"
    loc.country_code = "NL"
    loc.city = "Amsterdam"
    loc.network_zone = None
    provider = _provider()
    provider.list_locations = AsyncMock(return_value=[loc, loc])
    session = FakeSyncSession(
        [
            _scalars_result(None),  # provider row missing
            _scalars_result(None),  # loc 1 new
            _scalars_result(None),  # loc 2 new
        ]
    )
    syncer = LeaseWebCatalogSyncer(lambda: session, provider)
    result = await syncer.sync_locations()
    assert result.total_fetched == 2
    assert result.total_upserted == 2
    assert result.total_skipped == 0
    assert result.errors == []
    assert session.committed
    assert session.flushed
    assert len(session.added) == 3  # provider + 2 locations


async def test_sync_locations_updates_existing_rows() -> None:
    from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer

    loc = MagicMock()
    loc.id = "AMS-01"
    loc.name = "Amsterdam"
    loc.country_code = "NL"
    loc.city = None
    loc.network_zone = None
    provider = _provider()
    provider.list_locations = AsyncMock(return_value=[loc])
    found = MagicMock()
    session = FakeSyncSession(
        [
            _scalars_result("provider-uuid"),  # provider exists
            _scalars_result(found),  # location exists
        ]
    )
    syncer = LeaseWebCatalogSyncer(lambda: session, provider)
    result = await syncer.sync_locations()
    assert result.total_fetched == 1
    assert result.total_upserted == 0
    assert result.total_skipped == 1
    assert found.name == "Amsterdam"
    assert found.country_code == "NL"


async def test_sync_locations_provider_error() -> None:
    from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer

    provider = _provider()
    provider.list_locations = AsyncMock(side_effect=RuntimeError("api down"))
    syncer = LeaseWebCatalogSyncer(lambda: None, provider)
    result = await syncer.sync_locations()
    assert result.total_fetched == 0
    assert result.errors == ["api down"]


async def test_sync_plans_ingests_per_region(monkeypatch: pytest.MonkeyPatch) -> None:
    import cloud_platform.providers.leaseweb.sync as sync_mod
    from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer

    loc = MagicMock()
    loc.id = "AMS-01"
    plan = MagicMock()
    plan.id = "lsw.mini"
    plan.name = "Mini"
    plan.architecture = "x86"
    plan.vcpu = 1
    plan.memory_mb = 1024
    plan.disk_gb = 25
    plan.metadata = {
        "price_per_hour": "0.015",
        "price_per_month": "10.00",
        "currency": "EUR",
    }
    provider = _provider()
    provider.list_locations = AsyncMock(return_value=[loc])
    provider.list_plans = AsyncMock(return_value=[plan])

    class FakeIngestion:
        def __init__(self, repo: object) -> None:
            self.repo = repo

        async def ingest_plan(self, provider_key: str, pricing: object) -> Any:
            created = MagicMock()
            created.created = True
            skipped = MagicMock()
            skipped.created = False
            result = MagicMock()
            result.prices = [created, skipped]
            return result

    monkeypatch.setattr(sync_mod, "PricingIngestionService", FakeIngestion)
    syncer = LeaseWebCatalogSyncer(lambda: MagicMock(), provider)
    result = await syncer.sync_plans()
    assert result.total_fetched == 1
    assert result.total_upserted == 1
    assert result.total_skipped == 1
    assert result.errors == []


async def test_sync_plans_errors_are_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    import cloud_platform.providers.leaseweb.sync as sync_mod
    from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer

    loc = MagicMock()
    loc.id = "AMS-01"
    plan = MagicMock()
    plan.id = ""
    plan.name = ""
    plan.metadata = {}
    provider = _provider()
    provider.list_locations = AsyncMock(return_value=[loc])
    provider.list_plans = AsyncMock(return_value=[plan])
    monkeypatch.setattr(
        sync_mod,
        "PricingIngestionService",
        lambda repo: MagicMock(ingest_plan=AsyncMock()),
    )
    syncer = LeaseWebCatalogSyncer(lambda: MagicMock(), provider)
    result = await syncer.sync_plans()
    assert result.total_upserted == 0
    assert any("missing id" in e for e in result.errors)


async def test_sync_plans_locations_error() -> None:
    from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer

    provider = _provider()
    provider.list_locations = AsyncMock(side_effect=RuntimeError("boom"))
    syncer = LeaseWebCatalogSyncer(lambda: None, provider)
    result = await syncer.sync_plans()
    assert result.total_fetched == 0
    assert result.errors == ["locations: boom"]


async def test_sync_all_combines_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    from cloud_platform.providers.leaseweb.sync import LeaseWebCatalogSyncer

    provider = _provider()
    provider.list_locations = AsyncMock(return_value=[])
    provider.list_plans = AsyncMock(return_value=[])
    session = FakeSyncSession([_scalars_result("provider-uuid")])
    syncer = LeaseWebCatalogSyncer(lambda: session, provider)
    result = await syncer.sync_all()
    assert set(result) == {"locations", "plans"}


async def test_build_catalog_sync_job_steps() -> None:
    from cloud_platform.providers.leaseweb.sync import (
        LeaseWebCatalogSyncer,
        build_catalog_sync_job,
    )

    syncer = MagicMock(spec=LeaseWebCatalogSyncer)
    syncer.sync_locations = AsyncMock(
        return_value=type(
            "R",
            (),
            {"total_fetched": 1, "total_upserted": 1, "total_skipped": 0, "errors": []},
        )()
    )
    syncer.sync_plans = AsyncMock(
        return_value=type(
            "R",
            (),
            {"total_fetched": 2, "total_upserted": 0, "total_skipped": 2, "errors": ["e"]},
        )()
    )
    job = build_catalog_sync_job(syncer, MagicMock())
    assert [step.name for step in job._steps] == [
        "leaseweb-locations",
        "leaseweb-plans",
    ]
    report = await job.run()
    assert report.ran is True
    by_name = {r.name: r for r in report.steps}
    assert by_name["leaseweb-locations"].fetched == 1
    assert by_name["leaseweb-locations"].upserted == 1
    assert by_name["leaseweb-plans"].errors == ("e",)
