"""Error-mapping, retry and pagination branches of HetznerCloudProvider.

Uses httpx.MockTransport like tests/unit/test_hetzner_backoff.py (no
network): every response below is shaped like the official Hetzner Cloud
API (``{"server": ...}`` / ``{"servers": ..., "meta": {"pagination": ...}}``
envelopes and ``{"error": {"code", "message"}}`` failures).
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import CreateServerRequest, OrderRecoveryVerdict
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderCapacityError,
    ProviderConflict,
    ProviderError,
    ProviderOutcomeUnknown,
    ProviderRejected,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.backoff import RateLimitBackoff, RateLimitPolicy
from cloud_platform.providers.hetzner.client import HetznerCloudProvider, operation_label

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

    async def test_delete_server_proves_absence_before_mutating(self) -> None:
        requests: list[str] = []
        visible = True

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal visible
            requests.append(request.method)
            if request.method == "GET":
                if visible:
                    return httpx.Response(200, json={"server": _server_item()})
                return _error(404, "not_found", "already gone")
            assert request.method == "DELETE"
            visible = False
            return httpx.Response(200, json={"action": {"id": 1}})

        provider = _provider(respond)
        try:
            await provider.delete_server("123", IdempotencyKey("op-delete-1"))
            await provider.delete_server("123", IdempotencyKey("op-delete-1"))
        finally:
            await provider.close()
        assert requests == ["GET", "DELETE", "GET"]


def _inventory(
    rows: list[dict[str, Any]], *, page: int = 1, next_page: int | None = None
) -> dict[str, Any]:
    return {"servers": rows, "meta": {"pagination": {"page": page, "next_page": next_page}}}


class TestStrictProjectInventory:
    async def test_counts_off_and_manual_servers_across_all_pages(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            calls.append(page)
            return httpx.Response(
                200,
                json=_inventory(
                    [_server_item(id=page, status="off", labels={})],
                    page=page,
                    next_page=2 if page == 1 else None,
                ),
            )

        provider = _provider(handler)
        try:
            assert await provider.project_server_count() == 2
            assert calls == [1, 2]
        finally:
            await provider.close()

    @pytest.mark.parametrize(
        "payload",
        [
            {"servers": []},
            {"servers": [], "meta": {"pagination": {"page": 1}}},
            _inventory([{"id": True}]),
            _inventory([{"id": ""}]),
            _inventory([{"id": -1}]),
            _inventory([{"id": 1}, {"id": 1}]),
            _inventory([], next_page=1),
            _inventory([], next_page=3),
            _inventory([], page=2),
            {
                "servers": [],
                "meta": {"pagination": {"page": 1, "next_page": None, "total_entries": 2}},
            },
            {"servers": [], "meta": {"pagination": {"page": 1, "next_page": None, "last_page": 2}}},
        ],
    )
    async def test_malformed_scan_is_neither_capacity_nor_absence_proof(
        self, payload: dict
    ) -> None:
        provider = _provider(lambda request: httpx.Response(200, json=payload))
        try:
            with pytest.raises(ProviderError):
                await provider.project_server_count()
            recovered = await provider.recover_server_by_operation(
                "order-create:123", legacy_label=False
            )
            assert recovered.verdict == OrderRecoveryVerdict.SCAN_FAILED
        finally:
            await provider.close()

    async def test_duplicate_identity_across_pages_fails(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params["page"])
            return httpx.Response(
                200,
                json=_inventory(
                    [{"id": 1}],
                    page=page,
                    next_page=2 if page == 1 else None,
                ),
            )

        provider = _provider(handler)
        try:
            with pytest.raises(ProviderError, match="duplicate"):
                await provider.project_server_count()
        finally:
            await provider.close()


class TestDocumentedCapacity:
    @pytest.mark.parametrize(
        "status,code,expected",
        [
            (403, "resource_limit_exceeded", ProviderCapacityError),
            (403, "forbidden", ProviderAuthError),
            (401, "resource_limit_exceeded", ProviderAuthError),
            (422, "resource_limit_exceeded", ProviderError),
            (412, "resource_unavailable", ProviderError),
            (429, "resource_limit_exceeded", ProviderOutcomeUnknown),
        ],
    )
    async def test_only_documented_pre_acceptance_quota_is_capacity(
        self, status: int, code: str, expected: type[Exception]
    ) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(
                status,
                json={
                    "error": {
                        "code": code,
                        "details": {
                            "limits": [
                                {"name": "project_limit"},
                                {"name": "project_limit"},
                                {"name": ""},
                                {"name": 12},
                                {"name": "bad\nname"},
                                {},
                            ]
                        },
                    }
                },
            )

        provider = _provider(handler)
        try:
            with pytest.raises(expected) as caught:
                await provider.create_server(_create_request(), IdempotencyKey("order-create:123"))
            assert calls == ["POST"]
            if expected is ProviderCapacityError:
                assert caught.value.error_code == "resource_limit_exceeded"
                assert caught.value.quota_names == ("project_limit",)
                assert caught.value.retryable is False
            else:
                assert not isinstance(caught.value, ProviderCapacityError)
        finally:
            await provider.close()


class TestMonthlyIdentityAndRecovery:
    async def test_digest_label_and_initializing_response_are_accepted(self) -> None:
        key = "order-create:" + "a" * 100
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            body = json.loads(request.content)
            assert (
                body["labels"]["platform-operation"]
                == "op-" + sha256(key.encode()).hexdigest()[:60]
            )
            return httpx.Response(
                201,
                json={
                    "server": _server_item(
                        name=body["name"],
                        status="initializing",
                        labels=body["labels"],
                        image={"id": 100, "name": body["image"]},
                        server_type={"id": 1, "name": body["server_type"]},
                        datacenter={"location": {"name": body["location"]}},
                    )
                },
            )

        provider = _provider(handler)
        try:
            created = await provider.create_server(_create_request(), IdempotencyKey(key))
            assert (created.id, created.status) == ("123", "initializing")
            assert calls == ["POST"]
        finally:
            await provider.close()

    @pytest.mark.parametrize(
        "failure",
        [
            "transport",
            408,
            429,
            500,
            503,
            "json",
            "shape",
            "id",
            "name",
            "image",
            "plan",
            "location",
            "labels",
        ],
    )
    async def test_uncertain_create_is_never_sent_twice(self, failure: str | int) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if failure == "transport":
                raise httpx.ReadTimeout("unconfirmed", request=request)
            if isinstance(failure, int):
                return _error(failure)
            if failure == "json":
                return httpx.Response(201, content=b"invalid-json")
            if failure == "shape":
                return httpx.Response(201, json=[])
            body = json.loads(request.content)
            item = _server_item(
                name=body["name"],
                labels=body["labels"],
                image={"name": body["image"]},
                server_type={"name": body["server_type"]},
                datacenter={"location": {"name": body["location"]}},
            )
            field = {"plan": "server_type", "location": "datacenter"}.get(failure, failure)
            item[field] = None
            return httpx.Response(201, json={"server": item})

        provider = _provider(handler)
        try:
            with pytest.raises(ProviderOutcomeUnknown):
                await provider.create_server(_create_request(), IdempotencyKey("order-create:123"))
            assert calls == ["POST"]
        finally:
            await provider.close()

    @pytest.mark.parametrize("legacy", [True, False])
    async def test_exact_correlation_reads_all_pages_without_label_selector(
        self, legacy: bool
    ) -> None:
        key = "order-create:" + "b" * 80
        label = key[:63] if legacy else operation_label(key)
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert "label_selector" not in request.url.params
            page = int(request.url.params["page"])
            calls.append(page)
            labels = {"platform-operation": label, "platform_server_id": "local-123"}
            return httpx.Response(
                200,
                json=_inventory(
                    [
                        _server_item(
                            id=page,
                            labels=labels if page == 2 else {"platform-operation": label + "x"},
                        )
                    ],
                    page=page,
                    next_page=2 if page == 1 else None,
                ),
            )

        provider = _provider(handler)
        try:
            result = await provider.recover_server_by_operation(
                key, legacy_label=legacy, platform_server_id="local-123"
            )
            assert result.verdict == OrderRecoveryVerdict.MATCHED
            assert result.provider_order_id == "2"
            assert calls == [1, 2]
        finally:
            await provider.close()

    @pytest.mark.parametrize(
        "rows,expected",
        [
            ([], OrderRecoveryVerdict.NO_MATCH),
            (
                [
                    {
                        "id": 1,
                        "labels": {"platform-operation": "old", "platform_server_id": "other"},
                    },
                ],
                OrderRecoveryVerdict.AMBIGUOUS,
            ),
            (
                [
                    {"id": 1, "labels": {"platform-operation": "old"}},
                    {"id": 2, "labels": {"platform-operation": "old"}},
                ],
                OrderRecoveryVerdict.AMBIGUOUS,
            ),
        ],
    )
    async def test_conflicting_or_multiple_matches_are_not_owned(
        self, rows: list[dict], expected
    ) -> None:
        provider = _provider(lambda request: httpx.Response(200, json=_inventory(rows)))
        try:
            result = await provider.recover_server_by_operation("old", platform_server_id="local")
            assert result.verdict == expected
            assert result.provider_order_id is None
        finally:
            await provider.close()


@pytest.mark.asyncio
async def test_new_receipt_recovery_never_accepts_the_legacy_raw_label() -> None:
    key = "order-create:123"
    provider = _provider(
        lambda request: httpx.Response(
            200,
            json=_inventory(
                [
                    _server_item(labels={"platform-operation": key}),
                ]
            ),
        )
    )
    try:
        assert (await provider.recover_server_by_operation(key, legacy_label=False)).verdict == (
            OrderRecoveryVerdict.NO_MATCH
        )
        assert (
            await provider.recover_server_by_operation(key)
        ).verdict == OrderRecoveryVerdict.MATCHED
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_matching_first_page_does_not_hide_failed_later_inventory() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=_inventory(
                    [
                        _server_item(labels={"platform-operation": "old"}),
                    ],
                    next_page=2,
                ),
            )
        return httpx.Response(200, json={"servers": []})

    provider = _provider(handle)
    try:
        result = await provider.recover_server_by_operation("old")
        assert result.verdict == OrderRecoveryVerdict.SCAN_FAILED
        assert result.provider_order_id is None
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_health_and_candidate_verification_use_supported_locations() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"locations": []})

    provider = _provider(handle)
    try:
        assert (await provider.check_health()).value == "healthy"
        await provider.verify_credential("synthetic-candidate")
        assert calls == ["/v1/locations", "/v1/locations"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_documented_412_is_definitive_refusal_not_unknown_or_account_capacity() -> None:
    mutations = []

    def handle(request: httpx.Request) -> httpx.Response:
        mutations.append(request)
        return _error(412, "resource_unavailable", "The requested resource is unavailable")

    provider = _provider(handle)
    try:
        with pytest.raises(ProviderRejected):
            await provider.create_server(_create_request(), IdempotencyKey("create-412"))
        assert len(mutations) == 1
    finally:
        await provider.close()
