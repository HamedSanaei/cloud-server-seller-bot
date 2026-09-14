"""Leaseweb ordering catalog sync tests (LEASEWEB-MVP).

The syncer's repo classes are patched at the syncer-module level (they are
imported there at module import time) and the provider is faked, so the
full sync_all pipeline — locations, products, availability flags — runs
without a database or network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from cloud_platform.providers.leaseweb.errors import (
    LeasewebErrorPayload,
    LeasewebForbiddenError,
)
from cloud_platform.providers.leaseweb.ordering import (
    LeaseWebOrderingProvider,
    LocationEligibility,
    LocationProbe,
)
from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer

#: Deliberately fake test credential (never a real key; never leaves tests).
SYNC_TEST_KEY = "LSW-SYNC-TEST-KEY"


@dataclass(frozen=True)
class _FakeLocation:
    id: str = "AMS-01"
    name: str = "AMS-01"
    country_code: str = "NL"
    city: str | None = "Amsterdam"


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
    discovery_seeds = ("AMS-01",)

    def __init__(self, **kw: Any) -> None:
        self._contract_term = "1_MONTH"
        self._billing_cycle = "1_MONTH"

    async def list_locations(self) -> list[Any]:
        return [_FakeLocation()]

    def describe_location(self, code: str) -> Any:
        return _FakeLocation(id=code, name=code)

    async def list_products_unscoped(self) -> list[Any]:
        return []

    async def probe_location(self, location: str) -> Any:
        if location != "AMS-01":
            return LocationProbe(location, LocationEligibility.ELIGIBLE_EMPTY, (), (), "empty")
        return LocationProbe(
            location, LocationEligibility.ELIGIBLE_AVAILABLE, (_FakeProduct(),), (), "1 products"
        )

    async def list_products(self, location_id: str) -> list[Any]:
        return [_FakeProduct()]

    async def get_product(self, location_id: str, product_id: str) -> Any:
        return _FakeDetail()


def _fake_repo(return_value: Any = None) -> AsyncMock:
    repo = AsyncMock()
    repo.upsert = AsyncMock(return_value=1)
    repo.upsert_from_provider = AsyncMock(return_value=MagicMock(id="o1"))
    repo.mark_unavailable = AsyncMock(return_value=0)
    repo.list_all = AsyncMock(return_value=[])
    repo.list_for_provider = AsyncMock(return_value=[])
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
            async def probe_location(self, location: str) -> Any:
                if location == "AMS-01":
                    raise RuntimeError("products endpoint down")
                return LocationProbe(location, LocationEligibility.ELIGIBLE_EMPTY, (), (), "empty")

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


def _probe_result(
    location: str,
    eligibility: LocationEligibility,
    products: tuple[Any, ...] = (),
    discovered: tuple[str, ...] = (),
) -> LocationProbe:
    return LocationProbe(location, eligibility, products, discovered, eligibility.value)


class _DiscoveryProvider(_FakeProvider):
    """Scripted probe outcomes per location for discovery tests."""

    def __init__(self, probes: dict[str, Any], **kw: Any) -> None:
        super().__init__(**kw)
        self._probes = probes
        self.probed: list[str] = []

    async def probe_location(self, location: str) -> Any:
        self.probed.append(location)
        outcome = self._probes.get(location, ("empty",))
        if isinstance(outcome, Exception):
            raise outcome
        kind = outcome[0] if isinstance(outcome, tuple) else outcome
        if kind == "ok":
            return LocationProbe(
                location, LocationEligibility.ELIGIBLE_AVAILABLE, (_FakeProduct(),), (), "1"
            )
        return LocationProbe(location, LocationEligibility.ELIGIBLE_EMPTY, (), (), "empty")


class TestDynamicEligibilityDiscovery:
    async def test_ineligible_location_hides_without_global_failure(self, monkeypatch: Any) -> None:
        _patch_repos(monkeypatch)

        class _Provider(_DiscoveryProvider):
            async def probe_location(self, location: str) -> Any:
                self.probed.append(location)
                if location == "AMS-01":
                    return LocationProbe(
                        location,
                        LocationEligibility.INELIGIBLE_ACCOUNT,
                        (),
                        ("FRA-01", "FRA-10", "FRA-14"),
                        "not enabled for this sales organization",
                    )
                return await super().probe_location(location)

        provider = _Provider({"FRA-01": ("ok",)})
        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), provider)  # type: ignore[arg-type]
        result = await syncer.sync_all()
        # AMS-01 was probed and hidden; FRA-01 (named by the denial) was
        # discovered and probed in the same run and sells. No global failure.
        assert "AMS-01" in provider.probed
        assert "FRA-01" in provider.probed
        assert result["products"].total_upserted == 1
        assert not any("authentication" in error for error in result["products"].errors)

    async def test_transient_failure_preserves_last_known_offers(self, monkeypatch: Any) -> None:
        repos = _patch_repos(monkeypatch)
        from cloud_platform.providers.errors import ProviderUnavailable

        current = MagicMock(
            provider_key="leaseweb",
            product_id="VPS02_1",
            location_id="AMS-01",
            provider_available=True,
        )
        repos["offers"].list_all = AsyncMock(return_value=[current])

        class _Provider(_DiscoveryProvider):
            async def probe_location(self, location: str) -> Any:
                self.probed.append(location)
                if location == "AMS-01":
                    raise ProviderUnavailable("timeout")
                return await super().probe_location(location)

        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _Provider({}))  # type: ignore[arg-type]
        result = await syncer.sync_all()
        # The transient location keeps its last-known availability: the only
        # pair handed to mark_unavailable is the preserved one.
        marked = repos["offers"].mark_unavailable.await_args.args
        assert ("VPS02_1", "AMS-01") in marked[1]
        assert result["products"].total_upserted == 0

    async def test_empty_eligible_location_creates_no_offers(self, monkeypatch: Any) -> None:
        _patch_repos(monkeypatch)
        syncer = LeaseWebOrderingCatalogSyncer(  # type: ignore[arg-type]
            lambda: MagicMock(), _DiscoveryProvider({})
        )
        result = await syncer.sync_all()
        # Every candidate probed empty: nothing to sell, nothing hidden loudly.
        assert result["products"].total_upserted == 0
        assert result["products"].total_fetched == 0

    async def test_newly_discovered_location_is_probed_automatically(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_repos(monkeypatch)

        class _Detail:
            product = _FakeProduct()
            # A location nobody configured: must be persisted AND probed.
            available_locations = ("AMS-01", "FRA-10")

        class _Provider(_DiscoveryProvider):
            async def get_product(self, location_id: str, product_id: str) -> Any:
                return _Detail()

        provider = _Provider({})
        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), provider)  # type: ignore[arg-type]
        await syncer.sync_all()
        assert "FRA-10" in provider.probed
        upserted_locations = [
            call.args[0].location_id for call in repos["locations"].upsert.await_args_list
        ]
        assert "FRA-10" in upserted_locations

    async def test_unscoped_failure_does_not_fail_sync(self, monkeypatch: Any) -> None:
        _patch_repos(monkeypatch)

        class _Provider(_DiscoveryProvider):
            async def list_products_unscoped(self) -> list[Any]:
                raise RuntimeError("unscoped not supported here")

        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _Provider({"AMS-01": ("ok",)}))  # type: ignore[arg-type]
        result = await syncer.sync_all()
        assert result["products"].total_upserted == 1

    async def test_authentication_failure_aborts_without_hiding(self, monkeypatch: Any) -> None:
        from cloud_platform.providers.leaseweb.errors import LeasewebAuthenticationError

        repos = _patch_repos(monkeypatch)

        class _Provider(_DiscoveryProvider):
            async def probe_location(self, location: str) -> Any:
                raise LeasewebAuthenticationError("bad key")

        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), _Provider({}))  # type: ignore[arg-type]
        result = await syncer.sync_all()
        assert any("authentication failed" in error for error in result["products"].errors)
        repos["offers"].mark_unavailable.assert_not_awaited()


class TestSharedDiscoveryPipeline:
    """Manual sync, worker refresh and container share one construction."""

    def test_worker_and_cli_use_the_shared_provider_factory(self) -> None:
        import inspect

        import cloud_platform.cli as cli_module
        import cloud_platform.worker.settings as worker_settings

        assert "ordering_provider_from_settings" in inspect.getsource(
            worker_settings.sync_leaseweb_offers
        )
        assert "ordering_provider_from_settings" in inspect.getsource(
            cli_module.leaseweb_sync_offers
        )


class _ScriptedTransport:
    """Fake transport: per-location catalog outcomes for the real provider."""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self._outcomes = outcomes
        self.throttle = None

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert method == "GET", f"discovery must be read-only, got {method}"
        params = kwargs.get("params") or {}
        outcome = self._outcomes.get(str(params.get("location")), ([], 0))
        if isinstance(outcome, Exception):
            raise outcome
        items, total = outcome
        return {"vpss": items, "_metadata": {"totalCount": total}}

    async def aclose(self) -> None:
        return None


def _live_product(pid: str = "VPS02_1") -> dict[str, Any]:
    return {
        "id": pid,
        "name": "VPS",
        "vCpu": "4",
        "vRam": "6",
        "nvmeStorage": "100 GB",
        "traffic": "30 TB",
        "price": {"currency": "EUR", "total": 3.59},
    }


def _live_detail(pid: str = "VPS02_1", locations: tuple[str, ...] = ("FRA-01",)) -> dict[str, Any]:
    item = _live_product(pid)
    item["location"] = list(locations)
    item["configurationOptions"] = {"operatingSystem": [{"name": "Ubuntu 24.04", "price": 0}]}
    return {"vps": item}


class _LiveProvider(LeaseWebOrderingProvider):
    """Real adapter on a scripted transport (no network, real classification)."""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        super().__init__(api_key=SYNC_TEST_KEY, locations=("FRA-01", "AMS-01"))
        self._transport = _ScriptedTransport(outcomes)  # type: ignore[assignment]

    async def get_product(self, location: str, product_id: str) -> Any:
        outcome = self._transport._outcomes.get(f"detail:{location}:{product_id}")
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return await super().get_product(location, product_id)
        return outcome


class TestLiveEndToEndDiscovery:
    """Real probe classification + real sync handling, scripted transport."""

    async def test_account_403_hides_location_without_global_failure(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_repos(monkeypatch)
        denial = LeasewebForbiddenError(
            "You can only use this resource in your Sales Organization "
            "locations (FRA-01, FRA-10, FRA-14).",
            payload=LeasewebErrorPayload(http_status=403),
        )
        provider = _LiveProvider({"AMS-01": denial})
        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), provider)
        result = await syncer.sync_all()
        assert not any("authentication failed" in e for e in result["products"].errors)
        assert result["products"].total_upserted == 0
        repos["offers"].mark_unavailable.assert_awaited()

    async def test_eligible_location_flows_to_upsert(self, monkeypatch: Any) -> None:
        repos = _patch_repos(monkeypatch)
        provider = _LiveProvider(
            {
                "FRA-01": ([_live_product()], 1),
                "detail:FRA-01:VPS02_1": _detail_for_test(),
            }
        )
        syncer = LeaseWebOrderingCatalogSyncer(lambda: MagicMock(), provider)
        result = await syncer.sync_all()
        assert result["products"].total_upserted == 1
        update = repos["offers"].upsert_from_provider.await_args.kwargs["update"]
        assert update.provider_available is True
        assert update.provider_cost_minor > 0


def _detail_for_test() -> Any:
    """Build a real product detail through the real parser (no network)."""
    from cloud_platform.providers.leaseweb.ordering import (
        LeasewebProductDetail,
        _parse_product,
    )

    parsed = _parse_product(_live_detail()["vps"], "FRA-01")
    assert parsed is not None
    return LeasewebProductDetail(
        product=parsed,
        os_options=(),
        control_panels=(),
        disk_upgrades=(),
        slas=(),
        available_locations=("FRA-01",),
        contract_terms={},
        billing_cycles={},
    )
