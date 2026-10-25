"""Ordering discovery cannot feed legacy Public Cloud display rows back into HTTP."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.provider_routes.domain import RouteState
from cloud_platform.providers.leaseweb.discovery_cache import DiscoveryPolicy, current_policy
from cloud_platform.providers.leaseweb.ordering import (
    LeaseWebOrderingProvider,
    LocationEligibility,
    extract_location_codes,
    merge_candidates,
)
from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer
from cloud_platform.providers.leaseweb.transport import Throttle
from tests.unit.test_catalog_discovery_cache import MemoryCache
from tests.unit.test_leaseweb_ordering_api import PRODUCT_LIST_ITEM
from tests.unit.test_ordering_location_metadata import _LocationRepo
from tests.unit.test_ordering_sync import _patch_repos


@pytest.mark.parametrize("region", ["eu-west-3", "AP-SOUTHEAST-1", "US-EAST-1", "US-WEST-2"])
def test_datacenter_namespace_excludes_cloud_regions_without_a_seed_allowlist(region):
    assert merge_candidates((region, "PAR-99", " fra-10 ")) == ("PAR-99", "FRA-10")
    assert extract_location_codes(f"Regions {region}; datacenters PAR-99, FRA-10") == (
        "FRA-10",
        "PAR-99",
    )


async def test_direct_ordering_probe_of_cloud_region_is_unknown_without_http():
    provider = LeaseWebOrderingProvider("synthetic-location-key")
    provider._transport.request = AsyncMock(side_effect=AssertionError("unexpected HTTP"))
    try:
        result = await provider.probe_location("eu-west-3")
        assert result.eligibility is LocationEligibility.TRANSIENT_UNKNOWN
        provider._transport.request.assert_not_awaited()
    finally:
        await provider.close()


async def test_legacy_cloud_rows_and_routes_never_enter_multi_account_ordering(monkeypatch, caplog):
    repos = _patch_repos(monkeypatch)
    legacy = [
        LocationRecord(provider_key="leaseweb", location_id=code, name=code)
        for code in ("eu-west-3", "EU-WEST-3", "AP-SOUTHEAST-1", "PAR-99")
    ]
    locations = _LocationRepo(legacy)
    original = deepcopy(locations._rows)
    monkeypatch.setattr(
        "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
        lambda *_: locations,
    )
    repos["routes"].list_for_provider.return_value = [
        SimpleNamespace(
            location_id="US-WEST-2",
            credential_account_id="north",
            state=RouteState.TRANSIENT_UNKNOWN,
        )
    ]
    requests = []

    async def handler(request):
        assert request.method == "GET", "catalog sync attempted a provider mutation"
        account = request.headers["X-LSW-Auth"]
        location = request.url.params.get("location", "")
        requests.append((account, request.url.path, location))
        served = "FRA-10" if account == "north" else "LON-11"
        if request.url.path.endswith("/VPS02_1"):
            return httpx.Response(
                400,
                json={
                    "errorCode": 400,
                    "errorMessage": (
                        f"This product is not available in {location}. "
                        "Please choose another location."
                    ),
                },
            )
        if not location:
            # Even provider hints must be from the Ordering namespace.
            return httpx.Response(
                200,
                json={
                    "vpss": [
                        dict(PRODUCT_LIST_ITEM, location="US-EAST-1"),
                        dict(PRODUCT_LIST_ITEM, location="PAR-98"),
                    ],
                    "_metadata": {"totalCount": 2, "offset": 0, "limit": 100},
                },
            )
        if location not in (served, "PAR-99"):
            return httpx.Response(403, json={"errorMessage": "Sales Organization not enabled"})
        return httpx.Response(
            200,
            json={
                "vpss": [
                    {
                        "id": "VPS02_1",
                        "name": "VPS 2.1",
                        "vCpu": "2",
                        "vRam": "4",
                        "nvmeStorage": "100 GB",
                        "traffic": "20 TB",
                        "price": {"currency": "EUR", "basePrice": "9.99", "total": "9.99"},
                    }
                ],
                "_metadata": {"totalCount": 1, "offset": 0, "limit": 100},
            },
        )

    providers = {}
    cache = MemoryCache()
    try:
        for account in ("north", "uk"):
            provider = LeaseWebOrderingProvider(
                account,
                locations=("AP-SOUTHEAST-1",),
                throttle=Throttle(wait=AsyncMock()),
                max_retries=0,
            )
            await provider._client.aclose()
            provider._client = httpx.AsyncClient(
                base_url="https://leaseweb.test",
                headers={"X-LSW-Auth": account},
                transport=httpx.MockTransport(handler),
            )
            providers[account] = provider
        token = current_policy.set(DiscoveryPolicy(cache))
        try:
            syncer = LeaseWebOrderingCatalogSyncer(lambda: None, accounts=providers)
            first = await syncer.sync_products()
            cold_details = sum(path.endswith("/VPS02_1") for _, path, _ in requests)
            caplog.clear()
            second = await syncer.sync_products()
        finally:
            current_policy.reset(token)
        assert first.offers_persisted == second.offers_persisted == 3
        assert cold_details == 4  # two real sales-org locations plus PAR-99 per account
        assert sum(path.endswith("/VPS02_1") for _, path, _ in requests) == cold_details
        assert all(
            not location or location in merge_candidates((location,)) for _, _, location in requests
        )
        assert all(locations._rows[key] == row for key, row in original.items() if key != "PAR-99")
        assert "PAR-99" in locations._rows
        assert (
            "PAR-98" in locations._rows
        )  # new provider-discovered hall, absent from built-in seeds
        assert not [r for r in caplog.records if r.levelno >= 30]
    finally:
        for provider in providers.values():
            await provider.close()
