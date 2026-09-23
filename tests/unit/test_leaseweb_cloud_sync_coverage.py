"""Leaseweb hourly-cloud adapter + sync coverage (P2).

Behavior-driven completion for the branches the storefront flows do not
reach: provider-list failures, malformed payloads, reconciliation reads
(``list_instances`` / ``get_instance`` / ``find_by_reference``), settings
construction, sync persistence-failure isolation and the provider-neutral
``LeasewebHourlyCloudSyncSource`` report mapping.
"""

from __future__ import annotations

import unittest.mock as mock
from typing import Any

import pytest

from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFound,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.cloud import (
    CloudInstanceType,
    CloudRegion,
    LeasewebHourlyCloudProvider,
    hourly_provider_from_settings,
)
from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

PROVIDER_KEY = "leaseweb"


def _provider() -> LeasewebHourlyCloudProvider:
    provider = LeasewebHourlyCloudProvider.__new__(LeasewebHourlyCloudProvider)
    provider._transport = mock.AsyncMock()
    return provider


def _region(region_id: str = "r1") -> CloudRegion:
    """Provider-shaped region with its synced ISO country code."""
    return CloudRegion(id=region_id, name="R1", country_code="DE", city=None)


def _types_payload() -> dict[str, Any]:
    return {
        "instanceTypes": [
            {
                "name": "lsw.m6.large",
                "cpu": "4",
                "memory": "16384",
                "storage": "400",
                "pricePerHour": "0.0523",
                "currency": "eur",
                "category": "General Purpose",
            }
        ]
    }


# ---------------------------------------------------------------------------
# Adapter: list failures and malformed payloads
# ---------------------------------------------------------------------------


class TestAdapterFailureModes:
    async def test_region_timeout_maps_to_provider_unavailable(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(side_effect=ProviderUnavailable("timed out"))
        with pytest.raises(ProviderUnavailable):
            await provider.list_regions()

    async def test_instance_types_validation_error_propagates(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(side_effect=ProviderError("bad payload"))
        with pytest.raises(ProviderError):
            await provider.list_instance_types("eu-west-3")

    async def test_auth_error_propagates(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(side_effect=ProviderAuthError("401"))
        with pytest.raises(ProviderAuthError):
            await provider.list_instance_types("eu-west-3")

    async def test_list_instances_with_offset_param(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(
            return_value={
                "instances": [
                    {
                        "id": "i-1",
                        "reference": "srv-abc",
                        "state": "RUNNING",
                        "region": "eu-west-3",
                        "ipAddresses": [{"ip": "203.0.113.10", "version": "4"}],
                    }
                ]
            }
        )
        instances = await provider.list_instances("eu-west-3")
        assert len(instances) == 1
        assert instances[0].ipv4 == "203.0.113.10"
        method, path = provider._transport.request.call_args.args[:2]
        assert method == "GET" and path == "/publicCloud/v1/instances"

    async def test_get_instance_not_found_returns_none(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(side_effect=ProviderNotFound("absent"))
        assert await provider.get_instance("i-missing") is None

    async def test_get_instance_unwraps_envelope_and_parses(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(
            return_value={"instance": {"id": "i-2", "state": "ACTIVE"}}
        )
        found = await provider.get_instance("i-2")
        assert found is not None
        assert found.id == "i-2"
        assert found.state == "ACTIVE"

    async def test_get_instance_non_dict_returns_none(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(return_value=["unexpected"])
        assert await provider.get_instance("i-3") is None

    async def test_find_by_reference_matches_exact_reference(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(
            return_value={
                "instances": [
                    {"id": "a", "reference": "srv-xyz"},
                    {"id": "b", "reference": "srv-abc"},
                ]
            }
        )
        found = await provider.find_by_reference("eu-west-3", "srv-abc")
        assert found is not None
        assert found.id == "b"

    async def test_find_by_reference_absent_is_none(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(return_value={"instances": []})
        assert await provider.find_by_reference("eu-west-3", "srv-abc") is None

    async def test_find_by_reference_survives_missing_region(self) -> None:
        """Reconciliation reads fail soft: a region 404 means 'not found',
        never an exception out of the read-only reconciler."""
        provider = _provider()
        provider._transport.request = mock.AsyncMock(side_effect=ProviderNotFound("gone"))
        assert await provider.find_by_reference("eu-west-3", "srv-abc") is None

    async def test_images_parse_label_and_architecture(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(
            return_value={
                "images": [
                    {
                        "id": "ubuntu-24.04",
                        "displayName": "Ubuntu 24.04",
                        "os": "ubuntu",
                        "architecture": "x86_64",
                    },
                    {"id": "", "name": " "},  # no usable id -> skipped
                    "not-a-dict",
                ]
            }
        )
        images = await provider.list_images("eu-west-3")
        assert [image.id for image in images] == ["ubuntu-24.04"]
        assert images[0].label == "Ubuntu 24.04"
        assert images[0].architecture == "x86_64"


# ---------------------------------------------------------------------------
# Adapter: payload-parsing corners (pure functions via public lists)
# ---------------------------------------------------------------------------


class TestAdapterParsingCorners:
    async def test_regions_skip_entries_without_any_identifier(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(
            return_value={"regions": [{"country": "DE"}, {"id": "eu-west-3", "country": "de"}]}
        )
        regions = await provider.list_regions()
        assert [region.id for region in regions] == ["eu-west-3"]
        assert regions[0].country_code == "DE"

    async def test_instance_types_fail_closed_on_missing_price(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(
            return_value={
                "instanceTypes": [
                    {"name": "lsw.a", "cpu": "2", "memory": "4096"},  # no price
                    {"name": "lsw.b", "pricePerHour": "0.02", "currency": "EUR"},  # ok
                ]
            }
        )
        types = await provider.list_instance_types("eu-west-3")
        assert [item.id for item in types] == ["lsw.b"]

    async def test_instance_type_family_falls_back_to_other(self) -> None:
        provider = _provider()
        provider._transport.request = mock.AsyncMock(
            return_value={
                "instanceTypes": [
                    {"name": "lsw.mystery", "pricePerHour": "0.01", "currency": "EUR"}
                ]
            }
        )
        (item,) = await provider.list_instance_types("eu-west-3")
        assert item.family_key == "other"
        assert item.family_name

    async def test_unclassifiable_category_maps_to_other(self) -> None:
        item = CloudInstanceType(
            id="x",
            name="X",
            region="r1",
            vcpu=1,
            ram_gb=1,
            disk_gb=1,
            traffic=None,
            hourly_cost_minor=1,
            currency="EUR",
            family_key="other",
            family_name="Other",
            architecture=None,
            cpu_type=None,
            storage_type=None,
            ipv4=None,
            ipv6=None,
        )
        assert item.family_key == "other"

    async def test_hourly_provider_from_settings_prefers_default_key(self) -> None:
        from types import SimpleNamespace

        settings = SimpleNamespace(
            leaseweb_api_key="primary", leaseweb_accounts=[], leaseweb_timeout_seconds=30
        )
        built = hourly_provider_from_settings(settings)
        assert built is not None
        assert built.key == PROVIDER_KEY
        await built.close()

    async def test_hourly_provider_from_settings_falls_back_to_account(self) -> None:
        from types import SimpleNamespace

        account = SimpleNamespace(api_key="account-key")
        settings = SimpleNamespace(
            leaseweb_api_key="", leaseweb_accounts=[account], leaseweb_timeout_seconds=30
        )
        built = hourly_provider_from_settings(settings)
        assert built is not None
        await built.close()

    async def test_hourly_provider_from_settings_none_without_credential(self) -> None:
        from types import SimpleNamespace

        settings = SimpleNamespace(leaseweb_api_key="", leaseweb_accounts=[])
        assert hourly_provider_from_settings(settings) is None


# ---------------------------------------------------------------------------
# Sync: failure isolation and reconciliation guards
# ---------------------------------------------------------------------------


class _SyncProvider:
    def __init__(
        self,
        *,
        regions: list[Any] | None = None,
        types: dict[str, list[Any]] | None = None,
        regions_error: Exception | None = None,
        types_error: dict[str, Exception] | None = None,
    ) -> None:
        self._regions = regions or []
        self._types = types or {}
        self._regions_error = regions_error
        self._types_error = types_error or {}

    async def list_regions(self) -> list[Any]:
        if self._regions_error is not None:
            raise self._regions_error
        return self._regions

    async def list_instance_types(self, region: str) -> list[Any]:
        error = self._types_error.get(region)
        if error is not None:
            raise error
        return self._types.get(region, [])


def _cloud_type(type_id: str = "lsw.m6.large", region: str = "r1") -> CloudInstanceType:
    return CloudInstanceType(
        id=type_id,
        name=type_id,
        region=region,
        vcpu=4,
        ram_gb=16,
        disk_gb=400,
        traffic="1TB",
        hourly_cost_minor=5,
        currency="EUR",
        family_key="general-purpose",
        family_name="General Purpose",
        architecture="x86_64",
        cpu_type="shared",
        storage_type="nvme",
        ipv4=True,
        ipv6=True,
    )


class _SyncOffersRepo:
    def __init__(self, fail_on: set[str] | None = None, mark_error: Exception | None = None):
        self.upserts: list[tuple[str, str]] = []
        self.marked: list[tuple[str, str]] = []
        self._fail_on = fail_on or set()
        self._mark_error = mark_error

    async def upsert_from_provider(
        self, *, provider_key: str, product_id: str, location_id: str, update: Any
    ) -> None:
        if f"{product_id}/{location_id}" in self._fail_on:
            raise RuntimeError(f"db write failed for {product_id}/{location_id}")
        self.upserts.append((product_id, location_id))

    async def mark_unavailable(
        self, provider_key: str, available: set[tuple[str, str]], billing_model: str = ""
    ) -> int:
        if self._mark_error is not None:
            raise self._mark_error
        self.marked = sorted(available)
        return len(available)


class _LocationsRepo:
    def __init__(self, fail: bool = False) -> None:
        self.upserts: list[str] = []
        self._fail = fail

    async def upsert(self, record: Any) -> None:
        if self._fail:
            raise RuntimeError("location db down")
        self.upserts.append(record.location_id)


async def run_sync(provider: Any, repo: Any, locations: Any) -> Any:
    """Run one sync_all with faked repositories (patches live during the run)."""
    import cloud_platform.providers.leaseweb.cloud_sync as mod

    syncer = LeasewebHourlyCloudSyncer.__new__(LeasewebHourlyCloudSyncer)
    syncer._session_factory = None
    syncer._provider = provider
    with (
        mock.patch.object(mod, "SqlAlchemySellableOfferRepository", lambda sf: repo),
        mock.patch(
            "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
            lambda sf: locations,
        ),
    ):
        return await syncer.sync_all()


class TestSyncFailureIsolation:
    async def test_regions_error_returns_error_only_result(self) -> None:
        provider = _SyncProvider(regions_error=ProviderUnavailable("regions down"))
        repo = _SyncOffersRepo()
        result = await run_sync(provider, repo, _LocationsRepo())
        assert result.errors and "ProviderUnavailable" in result.errors[0]
        assert result.offers_written == 0
        assert repo.upserts == []

    async def test_empty_regions_is_reported(self) -> None:
        provider = _SyncProvider(regions=[])
        repo = _SyncOffersRepo()
        result = await run_sync(provider, repo, _LocationsRepo())
        assert result.errors == ["provider returned no readable regions"]
        assert repo.upserts == []

    async def test_location_metadata_failure_is_only_a_warning(self) -> None:
        provider = _SyncProvider(
            regions=[_region()],
            types={"r1": [_cloud_type()]},
        )
        repo = _SyncOffersRepo()
        result = await run_sync(provider, repo, _LocationsRepo(fail=True))
        assert result.offers_written == 1
        assert any("location metadata" in warning for warning in result.warnings)
        assert not result.persistence_failures

    async def test_region_persistence_failure_does_not_retire_inventory(self) -> None:
        provider = _SyncProvider(
            regions=[
                type("R", (), {"id": "r1", "name": "R1", "country_code": "DE", "city": None})(),
                type("R", (), {"id": "r2", "name": "R2", "country_code": "NL", "city": None})(),
            ],
            types={"r1": [_cloud_type()], "r2": [_cloud_type("lsw.m6.xl")]},
        )
        repo = _SyncOffersRepo(fail_on={"lsw.m6.large/r1"})
        result = await run_sync(provider, repo, _LocationsRepo())
        assert result.persistence_failures
        assert "nothing retired" in " ".join(result.warnings)
        assert result.marked_unavailable == 0  # reconciliation never ran
        assert sorted(repo.upserts) == [("lsw.m6.xl", "r2")]

    async def test_successful_full_sync_reconciles_availability(self) -> None:
        provider = _SyncProvider(
            regions=[_region()],
            types={"r1": [_cloud_type(), _cloud_type("lsw.m6.xl")]},
        )
        repo = _SyncOffersRepo()
        result = await run_sync(provider, repo, _LocationsRepo())
        assert result.offers_written == 2
        assert result.marked_unavailable == 2
        assert result.persistence_failures == ()
        assert ("lsw.m6.large", "r1") in result.verified

    async def test_mark_unavailable_failure_counts_as_persistence_failure(self) -> None:
        provider = _SyncProvider(
            regions=[_region()],
            types={"r1": [_cloud_type()]},
        )
        repo = _SyncOffersRepo(mark_error=RuntimeError("reconcile write failed"))
        result = await run_sync(provider, repo, _LocationsRepo())
        assert result.marked_unavailable == 0
        assert any("mark_unavailable" in failure for failure in result.persistence_failures)


# ---------------------------------------------------------------------------
# P2: the provider-neutral coordinator source (cloud_auto_sync.py)
# ---------------------------------------------------------------------------


class TestHourlyCloudSyncSource:
    def _source(self, sync_result: Any = None, error: Exception | None = None) -> Any:
        from cloud_platform.providers.leaseweb.cloud_auto_sync import (
            LeasewebHourlyCloudSyncSource,
        )

        syncer = mock.Mock()
        if error is not None:
            syncer.sync_all = mock.AsyncMock(side_effect=error)
        else:
            syncer.sync_all = mock.AsyncMock(return_value=sync_result)
        return LeasewebHourlyCloudSyncSource(syncer)

    def _result(self, **overrides: Any) -> Any:
        from cloud_platform.providers.leaseweb.cloud_sync import CloudSyncResult

        defaults: dict[str, Any] = {
            "regions": (mock.Mock(error=None),),
            "offers_written": 6,
            "marked_unavailable": 0,
            "warnings": (),
            "verified": frozenset({("a", "r1")}),
            "persistence_failures": (),
            "errors": [],
        }
        defaults.update(overrides)
        return CloudSyncResult(**defaults)

    async def test_success_maps_full_report(self) -> None:
        source = self._source(self._result())
        report = await source.sync_catalog()
        assert source.provider_key == PROVIDER_KEY
        assert report.ok is True
        assert report.complete is True
        assert report.billing_model == "hourly"
        assert report.discovered == 6
        assert report.persisted == 6
        assert report.verified == frozenset({("a", "r1")})

    async def test_provider_exception_maps_to_failed_report(self) -> None:
        source = self._source(error=ProviderUnavailable("down"))
        report = await source.sync_catalog()
        assert report.ok is False
        assert report.complete is False
        assert report.errors and "ProviderUnavailable" in report.errors[0]

    async def test_persistence_failures_fail_the_report(self) -> None:
        source = self._source(self._result(persistence_failures=("upsert x: boom",)))
        report = await source.sync_catalog()
        assert report.ok is False
        assert report.persistence_failures == ("upsert x: boom",)

    async def test_partial_region_failure_is_incomplete_but_written(self) -> None:
        region = mock.Mock(error="ProviderError")
        source = self._source(self._result(regions=(region,), offers_written=3))
        report = await source.sync_catalog()
        assert report.complete is False
        assert report.ok is True  # written > 0 and no global errors
        assert report.discovered == 3

    async def test_zero_regions_reported_incomplete_with_error(self) -> None:
        source = self._source(self._result(regions=()))
        report = await source.sync_catalog()
        assert report.complete is False
        assert any("no readable regions" in error for error in report.errors)

    async def test_zero_written_with_errors_fails(self) -> None:
        source = self._source(self._result(offers_written=0, errors=["r1: ProviderError"]))
        report = await source.sync_catalog()
        assert report.ok is False
