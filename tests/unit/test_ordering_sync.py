"""Leaseweb ordering catalog sync tests (LEASEWEB-MVP).

The syncer's repo classes are patched at the syncer-module level (they are
imported there at module import time) and the provider is faked, so the
full sync_all pipeline — locations, products, availability flags — runs
without a database or network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer


class _FakeLocation:
    id = "AMS-01"
    name = "AMS-01"
    country_code = "NL"
    city = "Amsterdam"


class _FakeProduct:
    id = "VPS02_1"
    name = "VPS S"
    vcpu = 2
    ram_gb = 4
    disk_gb = 100
    traffic = "10 TB"
    currency = "EUR"
    monthly_price_minor = 1299


class _FakeDetail:
    product = _FakeProduct()
    available_locations = ("AMS-01", "FRA-01")


class _FakeProvider:
    def __init__(self, **kw: Any) -> None:
        self._contract_term = "1_MONTH"
        self._billing_cycle = "1_MONTH"

    async def list_locations(self) -> list[Any]:
        return [_FakeLocation()]

    async def list_products(self, location_id: str) -> list[Any]:
        return [_FakeProduct()]

    async def get_product(self, location_id: str, product_id: str) -> Any:
        return _FakeDetail()


def _fake_repo(return_value: Any = None) -> AsyncMock:
    repo = AsyncMock()
    repo.upsert = AsyncMock(return_value=1)
    repo.upsert_from_provider = AsyncMock(return_value=MagicMock(id="o1"))
    repo.mark_unavailable = AsyncMock(return_value=0)
    return repo


def _patch_repos(monkeypatch: Any) -> dict[str, AsyncMock]:
    loc_repo = _fake_repo()
    offers_repo = _fake_repo()
    monkeypatch.setattr(
        "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
        lambda *a, **k: loc_repo,
    )
    monkeypatch.setattr(
        "cloud_platform.providers.leaseweb.ordering_sync.SqlAlchemySellableOfferRepository",
        lambda *a, **k: offers_repo,
    )
    return {"locations": loc_repo, "offers": offers_repo}


class TestOrderingCatalogSyncer:
    async def test_sync_all_upserts_locations_and_products(self, monkeypatch: Any) -> None:
        repos = _patch_repos(monkeypatch)
        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _FakeProvider())  # type: ignore[arg-type]
        result = await syncer.sync_all()
        assert result["locations"].total_fetched == 1
        assert result["locations"].total_upserted == 1
        assert result["products"].total_fetched == 1
        assert result["products"].total_upserted == 1
        # The upsert carried the provider cost, never a selling price.
        call = repos["offers"].upsert_from_provider.await_args.kwargs
        assert call["provider_key"] == "leaseweb"
        assert call["update"].provider_cost_minor == 1299
        assert call["update"].provider_available is True
        repos["offers"].mark_unavailable.assert_awaited_once()

    async def test_sync_marks_moved_products_unavailable(self, monkeypatch: Any) -> None:
        repos = _patch_repos(monkeypatch)

        class _MovedDetail:
            product = _FakeProduct()
            available_locations = ("FRA-01",)  # no longer sold at AMS-01

        class _Provider(_FakeProvider):
            async def get_product(self, location_id: str, product_id: str) -> Any:
                return _MovedDetail()

        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _Provider())  # type: ignore[arg-type]
        result = await syncer.sync_all()
        assert result["products"].total_upserted == 0
        moved = repos["offers"].upsert_from_provider.await_args.kwargs["update"]
        assert moved.provider_available is False

    async def test_sync_survives_location_and_product_failures(self, monkeypatch: Any) -> None:
        _patch_repos(monkeypatch)

        class _FlakyProvider(_FakeProvider):
            async def list_products(self, location_id: str) -> list[Any]:
                raise RuntimeError("products endpoint down")

            async def get_product(self, location_id: str, product_id: str) -> Any:
                raise RuntimeError("detail down")

        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _FlakyProvider())  # type: ignore[arg-type]
        result = await syncer.sync_all()
        assert result["products"].errors  # recorded, not raised
        assert result["locations"].total_upserted == 1

    async def test_sync_locations_failure_is_recorded(self, monkeypatch: Any) -> None:
        _patch_repos(monkeypatch)

        class _DownProvider(_FakeProvider):
            async def list_locations(self) -> list[Any]:
                raise RuntimeError("auth rejected")

        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _DownProvider())  # type: ignore[arg-type]
        result = await syncer.sync_locations()
        assert result.total_fetched == 0
        assert result.errors and "locations" in result.errors[0]

    async def test_sync_never_touches_operator_pricing(self, monkeypatch: Any) -> None:
        """Sync refreshes provider facts; enabled/selling price are operator
        fields and must never be written by the syncer."""
        repos = _patch_repos(monkeypatch)
        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _FakeProvider())  # type: ignore[arg-type]
        await syncer.sync_products()
        update = repos["offers"].upsert_from_provider.await_args.kwargs["update"]
        assert not hasattr(update, "enabled")
        assert not hasattr(update, "selling_price_minor")
        # The billing parameters record the configured contract/billing cycle.
        assert update.billing_parameters["contract_term"] == "1_MONTH"
        assert update.billing_parameters["billing_cycle"] == "1_MONTH"
