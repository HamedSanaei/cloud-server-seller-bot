"""Fake-HTTP tests for the LeaseWeb adapter.

Offline half of the provider contract suite: auth header, error mapping,
catalog reads, server CRUD + idempotency, no secret leakage.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import CreateServerRequest
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.client import (
    LeaseWebProvider,
    Throttle,
    normalize_provider_status,
)

KEY = "LSW-TEST-KEY-DO-NOT-LEAK"
IK = IdempotencyKey("test-key-leaseweb-1")


def _no_sleep() -> Throttle:
    async def wait(_s: float) -> None:
        return None

    return Throttle(max_rps=1000.0, wait=wait)


def _provider(handler: Any, base_url: str = "https://api.test") -> LeaseWebProvider:
    provider = LeaseWebProvider(api_key=KEY, base_url=base_url, throttle=_no_sleep(), max_retries=2)
    provider._client = AsyncMock()  # type: ignore[method-assign]
    provider._client.request = AsyncMock(side_effect=handler)
    return provider


def _response(status_code: int, payload: Any = None) -> httpx.Response:
    body = b"" if payload is None else json.dumps(payload).encode()
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json"},
        content=body,
        request=httpx.Request("GET", "https://api.test/x"),
    )


def _instance(
    item_id: str = "i-1", reference: str = "srv-1", state: str = "RUNNING"
) -> dict[str, Any]:
    return {
        "id": item_id,
        "reference": reference,
        "contract": {"id": 1},
        "state": state,
        "region": "AMS-01",
        "ips": [{"ip": "1.2.3.4", "version": "4"}],
    }


def _json_response(payload: Any = None) -> httpx.Response:
    return _response(200, payload)


def _error_response(status_code: int, message: str) -> httpx.Response:
    return _response(status_code, {"errorMessage": message})


class TestAuthAndTransport:
    def test_constructor_sets_lsw_auth_header(self) -> None:
        provider = LeaseWebProvider(api_key=KEY, base_url="https://x", throttle=_no_sleep())
        headers = {k.lower(): v for k, v in provider._client.headers.items()}
        assert headers["x-lsw-auth"] == KEY
        assert "authorization" not in headers

    async def test_key_never_leaks_into_errors(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(401, {"errorMessage": "Invalid API key"}))
        with pytest.raises(ProviderAuthError) as exc:
            await provider.get_server("i-1")
        assert KEY not in str(exc.value)

    async def test_error_mapping_table(self) -> None:
        cases = [
            (401, ProviderAuthError),
            (403, ProviderAuthError),
            (409, ProviderConflict),
            (500, ProviderUnavailable),
        ]
        for status, error_type in cases:
            provider = _provider(
                lambda m, p, status=status, **kw: _response(status, {"errorMessage": "x"})
            )
            with pytest.raises(error_type):
                await provider.get_server("i-1")

    async def test_404_get_returns_none(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(404, {"errorMessage": "gone"}))
        assert await provider.get_server("missing") is None

    async def test_429_raises_rate_limited(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(429, {"errorMessage": "slow down"}))
        with pytest.raises(ProviderRateLimited):
            await provider.get_server("i-1")


class TestCatalog:
    async def test_list_locations(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            assert path == "/publicCloud/v1/regions"
            return _response(200, {"regions": [{"name": "AMS-01", "country": "NL"}]})

        locations = await _provider(handler).list_locations()
        assert [loc.id for loc in locations] == ["AMS-01"]
        assert locations[0].country_code == "NL"

    async def test_list_plans(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(
                200,
                {
                    "instanceTypes": [
                        {"name": "lsw.mini", "cpu": 1, "memoryMb": 1024, "disk": 25},
                    ]
                },
            )

        plans = await _provider(handler).list_plans()
        assert plans[0].id == "lsw.mini"
        assert plans[0].vcpu == 1

    async def test_list_images(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(200, {"images": [{"id": "ubuntu-24", "name": "Ubuntu 24.04"}]})

        images = await _provider(handler).list_images()
        assert [i.id for i in images] == ["ubuntu-24"]


class TestServerLifecycle:
    async def test_get_server_maps_status(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(200, {"instance": _instance()}))
        server = await provider.get_server("i-1")
        assert server is not None
        assert server.status == "running"
        assert server.ipv4 == "1.2.3.4"

    async def test_get_server_404_returns_none(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(404, {"errorMessage": "gone"}))
        assert await provider.get_server("missing") is None

    async def test_create_is_idempotent_by_name(self) -> None:
        calls: list[str] = []

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            calls.append(f"{method} {path}")
            if method == "GET":
                return _response(200, {"instances": [_instance(reference="srv-1")]})
            raise AssertionError("POST must not be called when the name already exists")

        provider = _provider(handler)
        server = await provider.create_server(
            CreateServerRequest(
                name="srv-1", plan_id="lsw.mini", image_id="ubuntu-24", location_id="AMS-01"
            ),
            IK,
        )
        assert server is not None
        assert calls and calls[0].startswith("GET")

    async def test_delete_404_is_success(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(404, {"errorMessage": "gone"}))
        await provider.delete_server("i-1", IK)

    def test_status_normalization(self) -> None:
        assert normalize_provider_status("RUNNING") == "running"
        assert normalize_provider_status("POWERED-OFF") == "stopped"


class TestCatalogFallbacksAndHelpers:
    async def test_list_locations_falls_back_to_region(self) -> None:
        provider = LeaseWebProvider(api_key="k", region="REG-1")
        provider._client.request = AsyncMock(return_value=_json_response({"regions": []}))
        locations = await provider.list_locations()
        assert len(locations) == 1
        assert locations[0].id == "REG-1"
        assert locations[0].metadata["source"] == "configured-default"

    async def test_list_locations_not_found_returns_default_region(self) -> None:
        provider = LeaseWebProvider(api_key="k", region="FRA-2")
        provider._client.request = AsyncMock(
            side_effect=lambda *a, **k: _error_response(404, "no such endpoint")
        )
        locations = await provider.list_locations()
        assert len(locations) == 1
        assert locations[0].id == "FRA-2"

    async def test_list_plans_parses_resources_and_prices(self) -> None:
        provider = LeaseWebProvider(api_key="k", region="AMS")
        provider._client.request = AsyncMock(
            return_value=_json_response(
                {
                    "instanceTypes": [
                        {
                            "name": "vps.s.1",
                            "displayName": "Small VPS",
                            "cpu": 2,
                            "memoryMb": 4096,
                            "rootDiskSize": 80,
                            "architecture": "arm64",
                            "pricePerMonth": 12.99,
                        }
                    ]
                }
            )
        )
        plans = await provider.list_plans()
        assert len(plans) == 1
        plan = plans[0]
        assert plan.id == "vps.s.1"
        assert plan.architecture == "arm64"
        assert plan.vcpu == 2
        assert plan.memory_mb == 4096
        assert plan.disk_gb == 80
        assert plan.metadata["price_per_month"] == 12.99
        assert plan.metadata["region"] == "AMS"

    async def test_list_images_parses_os_family(self) -> None:
        provider = LeaseWebProvider(api_key="k")
        provider._client.request = AsyncMock(
            return_value=_json_response(
                {
                    "images": [
                        {
                            "id": "ubuntu-24.04",
                            "displayName": "Ubuntu 24.04",
                            "os": "ubuntu",
                            "version": "24.04",
                            "architecture": "x86_64",
                        }
                    ]
                }
            )
        )
        images = await provider.list_images()
        assert len(images) == 1
        assert images[0].id == "ubuntu-24.04"
        assert images[0].os_family == "ubuntu"
        assert images[0].metadata["os_version"] == "24.04"

    def test_memory_mb_gb_and_bad_values(self) -> None:
        from cloud_platform.providers.leaseweb.client import _memory_mb

        assert _memory_mb({"memoryGb": 2}, {}) == 2048
        assert _memory_mb({"memory_gb": "1.5"}, {}) == 1536
        assert _memory_mb({}, {"ram": "not-a-number"}) == 0
        assert _memory_mb({"memoryMb": 512}, {}) == 512

    def test_operation_label_shapes_uuids(self) -> None:
        from cloud_platform.providers.leaseweb.client import _operation_label

        assert (
            _operation_label(
                "GET",
                "/publicCloud/v1/instances/123e4567-e89b-12d3-a456-426614174000?region=AMS",
            )
            == "GET /publicCloud/v1/instances/{id}"
        )
        assert (
            _operation_label("POST", "/publicCloud/v1/instances")
            == "POST /publicCloud/v1/instances"
        )
        assert _operation_label("GET", "/publicCloud/v1/regions") == "GET /publicCloud/v1/regions"

    def test_parse_retry_after(self) -> None:
        from cloud_platform.providers.leaseweb.client import _parse_retry_after

        assert _parse_retry_after("2") == 2
        assert _parse_retry_after("-1") == 0
        assert _parse_retry_after("banana") is None
        assert _parse_retry_after(None) is None


class TestManagementActions:
    async def test_power_actions_post_correct_paths(self) -> None:
        provider = LeaseWebProvider(api_key="k")
        provider._client.request = AsyncMock(return_value=_json_response({}))

        await provider.power_on("svr-1", IK)
        await provider.power_off("svr-1", IK)
        await provider.reboot("svr-1", IK)
        calls = provider._client.request.await_args_list
        assert [c.args[1] for c in calls] == [
            "/publicCloud/v1/instances/svr-1/start",
            "/publicCloud/v1/instances/svr-1/stop",
            "/publicCloud/v1/instances/svr-1/reboot",
        ]

    async def test_create_server_with_ssh_and_user_data(self) -> None:
        provider = LeaseWebProvider(api_key="k", region="AMS")
        created = _json_response(
            {
                "instance": {
                    "id": "i-1",
                    "reference": "my-server",
                    "state": "running",
                    "ips": [{"ip": "1.2.3.4", "version": 4}],
                    "region": "AMS",
                }
            }
        )
        provider._client.request = AsyncMock(return_value=created)

        from cloud_platform.providers.base import CreateServerRequest

        request = CreateServerRequest(
            name="my-server",
            location_id="AMS",
            plan_id="vps.s.1",
            image_id="ubuntu-24.04",
            ssh_key_ids=("key-1",),
            user_data="#cloud-config",
            labels={"team": "x"},
        )
        server = await provider.create_server(request, IK)
        assert server.id == "i-1"
        assert server.ipv4 == "1.2.3.4"
        body = provider._client.request.await_args.kwargs["json"]
        assert body["sshKey"] == "key-1"
        assert body["userData"] == "#cloud-config"
        assert body["labels"]["team"] == "x"
        assert "platform-operation" in body["labels"]

    async def test_create_finds_existing_by_name(self) -> None:
        provider = LeaseWebProvider(api_key="k")
        existing = _json_response(
            {
                "instances": [
                    {
                        "id": "i-old",
                        "reference": "dup-name",
                        "state": "running",
                        "ips": [],
                    }
                ],
                "_metadata": {"totalCount": 1},
            }
        )
        provider._client.request = AsyncMock(return_value=existing)

        from cloud_platform.providers.base import CreateServerRequest

        request = CreateServerRequest(
            name="dup-name", location_id="AMS", plan_id="vps.s.1", image_id="ubuntu-24.04"
        )
        server = await provider.create_server(request, IK)
        assert server.id == "i-old"
        assert provider._client.request.await_args.args[0] == "GET"

    async def test_list_servers_paginates(self) -> None:
        provider = LeaseWebProvider(api_key="k")
        page1 = _json_response(
            {
                "instances": [
                    {"id": "a", "reference": "a", "state": "running", "ips": []},
                    {"id": "b", "reference": "b", "state": "running", "ips": []},
                ],
                "_metadata": {"totalCount": 3},
            }
        )
        page2 = _json_response(
            {
                "instances": [{"id": "c", "reference": "c", "state": "running", "ips": []}],
                "_metadata": {"totalCount": 3},
            }
        )
        provider._client.request = AsyncMock(side_effect=[page1, page2])
        servers = await provider.list_servers()
        assert [s.id for s in servers] == ["a", "b", "c"]
        assert provider._client.request.await_args_list[1].kwargs["params"]["offset"] == 2

    async def test_verify_credential_sends_candidate_only(self) -> None:
        provider = LeaseWebProvider(api_key="live-key")
        provider._client.request = AsyncMock(return_value=_json_response([]))
        await provider.verify_credential("candidate-key")
        call = provider._client.request.await_args
        assert call.kwargs["headers"]["X-LSW-Auth"] == "candidate-key"

    async def test_verify_credential_rejects_bad_key(self) -> None:
        provider = LeaseWebProvider(api_key="live-key")
        provider._client.request = AsyncMock(
            side_effect=lambda *a, **k: _error_response(401, "unauthorized")
        )
        with pytest.raises(ProviderAuthError):
            await provider.verify_credential("bad-key")

    async def test_credential_source_rotates_header(self) -> None:
        from cloud_platform.providers.credentials import Credential

        class Source:
            async def get(self) -> Credential:
                return Credential(value="rotated-key", key_hint="leaseweb")

        provider = LeaseWebProvider(api_key="init", credential_source=Source())
        provider._client.request = AsyncMock(return_value=_json_response([]))
        await provider.list_locations()
        call = provider._client.request.await_args
        assert call.kwargs["headers"]["X-LSW-Auth"] == "rotated-key"

    async def test_429_retries_with_backoff_then_succeeds(self) -> None:
        provider = LeaseWebProvider(api_key="k", max_retries=3)
        retry = _error_response(429, "slow down")
        retry.headers["Retry-After"] = "0"
        provider._client.request = AsyncMock(side_effect=[retry, _json_response([])])
        throttle = provider._throttle
        original_wait = throttle.wait

        async def fake_wait(seconds: float) -> None:
            assert seconds <= 30.0

        throttle.wait = fake_wait  # type: ignore[method-assign]
        await provider.list_locations()
        assert provider._client.request.await_count == 2
        throttle.wait = original_wait  # type: ignore[method-assign]

    async def test_network_error_maps_to_unavailable(self) -> None:
        provider = LeaseWebProvider(api_key="k", max_retries=0)
        provider._client.request = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(ProviderUnavailable):
            await provider.list_locations()

    def test_error_payload_variants(self) -> None:
        import httpx as _httpx

        from cloud_platform.providers.leaseweb.client import _error_payload

        assert _error_payload(_json_response({"errorMessage": "nope"})) == "nope"
        assert _error_payload(_json_response({"errorCode": "E42"})) == "leaseweb error E42"
        assert _error_payload(_json_response({"errors": ["one", "two"]})) == "one"
        assert _error_payload(_json_response({"unexpected": 1})) == "HTTP 200"
        assert _error_payload(_httpx.Response(503, text="<html>down</html>")) == "<html>down</html>"

    def test_constructor_validates_inputs(self) -> None:
        with pytest.raises(ValueError):
            LeaseWebProvider(api_key="")
        with pytest.raises(ValueError):
            LeaseWebProvider(api_key="k", max_retries=-1)
        with pytest.raises(ValueError):
            Throttle(max_rps=0)
