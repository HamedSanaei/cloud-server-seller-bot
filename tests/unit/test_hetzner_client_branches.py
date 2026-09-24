"""Error-mapping, retry and pagination branches of HetznerCloudProvider.

Uses httpx.MockTransport like tests/unit/test_hetzner_backoff.py (no
network): every response below is shaped like the official Hetzner Cloud
API (``{"server": ...}`` / ``{"servers": ..., "meta": {"pagination": ...}}``
envelopes and ``{"error": {"code", "message"}}`` failures).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import CreateServerRequest
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.backoff import RateLimitBackoff, RateLimitPolicy
from cloud_platform.providers.hetzner.client import HetznerCloudProvider

BASE = "https://api.hetzner.cloud/v1"


def _provider(
    handler: Any,
    policy: RateLimitPolicy | None = None,
    sleep_fn: Any | None = None,
) -> HetznerCloudProvider:
    provider = HetznerCloudProvider(token="t", rate_limit_policy=policy)
    if sleep_fn is not None:
        provider._backoff = RateLimitBackoff(policy or RateLimitPolicy(), sleep_fn=sleep_fn)
    provider._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    return provider


def _error(status: int, code: str = "boom", message: str = "failed") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}})


def _server_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": 123,
        "name": "web-1",
        "status": "running",
        "public_net": {"ipv4": {"ip": "1.2.3.4"}, "ipv6": {"ip": "2001:db8::1"}},
        "labels": {"platform-operation": "op-1"},
    }
    item.update(overrides)
    return item


def _create_request() -> CreateServerRequest:
    return CreateServerRequest(
        name="srv-1", plan_id="cx11", image_id="ubuntu-24.04", location_id="fsn1"
    )


class TestErrorMapping:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_failures_raise_auth_error(self, status: int) -> None:
        provider = _provider(lambda request: _error(status, "unauthorized", "bad token"))
        try:
            with pytest.raises(ProviderAuthError, match="bad token"):
                await provider._request("GET", "/servers/1")
        finally:
            await provider.close()

    async def test_get_server_not_found_returns_none(self) -> None:
        provider = _provider(lambda request: _error(404, "not_found", "server not found"))
        try:
            assert await provider.get_server("999") is None
        finally:
            await provider.close()

    @pytest.mark.parametrize("status", [409, 423])
    async def test_conflict_statuses_raise_conflict(self, status: int) -> None:
        provider = _provider(lambda request: _error(status, "locked", "in progress"))
        try:
            with pytest.raises(ProviderConflict, match="in progress"):
                await provider._request("POST", "/servers/1/actions/poweron")
        finally:
            await provider.close()

    async def test_server_error_raises_unavailable(self) -> None:
        provider = _provider(lambda request: _error(500, "server_error", "kaput"))
        try:
            with pytest.raises(ProviderUnavailable, match="kaput"):
                await provider._request("GET", "/servers")
        finally:
            await provider.close()

    async def test_generic_client_error_raises_provider_error(self) -> None:
        provider = _provider(lambda request: _error(422, "invalid_input", "bad input"))
        try:
            with pytest.raises(ProviderError, match="bad input"):
                await provider._request("POST", "/servers")
        finally:
            await provider.close()

    async def test_unexpected_json_shape_raises(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json=[1, 2, 3]))
        try:
            with pytest.raises(ProviderError, match="unexpected JSON shape"):
                await provider._request("GET", "/servers")
        finally:
            await provider.close()

    async def test_transport_failure_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        provider = _provider(handler)
        try:
            with pytest.raises(ProviderUnavailable, match="connection refused"):
                await provider._request("GET", "/servers")
        finally:
            await provider.close()


class TestCreateOutcomeUnknown:
    async def test_create_server_500_is_outcome_unknown_not_retryable(self) -> None:
        provider = _provider(lambda request: _error(500, "server_error", "dropped"))
        try:
            with pytest.raises(ProviderOutcomeUnknown, match="outcome unknown"):
                await provider.create_server(_create_request(), IdempotencyKey("op-create-1"))
        finally:
            await provider.close()


class TestPagination:
    async def test_list_servers_follows_next_page(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("page", "1")
            calls.append(page)
            if page == "1":
                return httpx.Response(
                    200,
                    headers={"RateLimit-Remaining": "99"},
                    json={
                        "servers": [_server_item(id=1)],
                        "meta": {"pagination": {"page": 1, "next_page": 2}},
                    },
                )
            return httpx.Response(
                200,
                headers={"RateLimit-Remaining": "98"},
                json={
                    "servers": [_server_item(id=2)],
                    "meta": {"pagination": {"page": 2, "next_page": None}},
                },
            )

        provider = _provider(handler)
        try:
            servers = await provider.list_servers()
        finally:
            await provider.close()

        assert [s.id for s in servers] == ["1", "2"]
        assert calls == ["1", "2"]
        assert provider.last_rate_limit.remaining == 98

    async def test_list_servers_stops_when_next_page_does_not_advance(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.params.get("page", "1"))
            return httpx.Response(
                200,
                json={
                    "servers": [_server_item(id=7)],
                    "meta": {"pagination": {"page": 1, "next_page": 1}},
                },
            )

        provider = _provider(handler)
        try:
            servers = await provider.list_servers()
        finally:
            await provider.close()

        assert [s.id for s in servers] == ["7"]
        assert calls == ["1"]


class TestRetryPath:
    async def test_429_then_success_sleeps_once_and_returns(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(429, json={"error": {"message": "rate limit"}})
            return httpx.Response(200, json={"locations": []})

        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        provider = _provider(handler, sleep_fn=fake_sleep)
        try:
            result = await provider._request("GET", "/locations")
        finally:
            await provider.close()

        assert result == {"locations": []}
        assert len(calls) == 2
        assert sleeps == [0.5]


class TestServerMappingAndDelete:
    async def test_get_server_maps_official_payload(self) -> None:
        provider = _provider(lambda request: httpx.Response(200, json={"server": _server_item()}))
        try:
            server = await provider.get_server("123")
        finally:
            await provider.close()

        assert server is not None
        assert server.id == "123"
        assert server.name == "web-1"
        assert server.status == "running"
        assert server.ipv4 == "1.2.3.4"
        assert server.ipv6 == "2001:db8::1"
        assert server.metadata["labels"] == {"platform-operation": "op-1"}

    async def test_delete_server_treats_404_as_idempotent(self) -> None:
        gone = _provider(lambda request: _error(404, "not_found", "already gone"))
        try:
            assert await gone.delete_server("1", IdempotencyKey("op-delete-1")) is None
        finally:
            await gone.close()

        ok = _provider(lambda request: httpx.Response(200, json={}))
        try:
            assert await ok.delete_server("1", IdempotencyKey("op-delete-1")) is None
        finally:
            await ok.close()
