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
