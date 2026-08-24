"""Fake-HTTP tests for the ArvanCloud adapter (M15-002).

Acceptance: provider contract suite passes - here the portable, offline
half of it: fake-HTTP unit tests for every port method, the error-mapping
table, the platform-side idempotency/reconciliation behavior, and zero
secret leakage in test snapshots. Live-account contract tests run through
``ProviderContractTests`` once credentials exist (PROVIDER_CONTRACT.md
§10/§12).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.arvancloud.client import (
    ArvanCloudProvider,
    Throttle,
    normalize_provider_status,
    parse_server_id,
    qualify_server_id,
)
from cloud_platform.providers.base import CreateServerRequest
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)

REGION = "ir-thr-1"
KEY = "MU-TEST-KEY-DO-NOT-LEAK"
IK = IdempotencyKey("test-key")


def _no_sleep() -> Throttle:
    async def now() -> float:
        return 0.0

    async def wait(_s: float) -> None:
        return None

    return Throttle(max_rps=1000.0, wait=wait)


def _provider(
    handler: Any, region: str = REGION, base_url: str = "https://api.test/v1"
) -> ArvanCloudProvider:
    provider = ArvanCloudProvider(
        api_key=KEY,
        base_url=base_url,
        region=region,
        throttle=_no_sleep(),
        max_retries=2,
    )
    provider._client = AsyncMock()
    provider._client.request = AsyncMock(side_effect=handler)
    return provider


def _response(
    status_code: int,
    payload: Any = None,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
) -> httpx.Response:
    if content is not None:
        body = content
    elif payload is None:
        body = b""
    else:
        body = json.dumps(payload).encode()
    return httpx.Response(
        status_code,
        headers=headers or {"Content-Type": "application/json"},
        content=body,
        request=httpx.Request("GET", "https://api.test/v1/x"),
    )


def _server_item(raw_id: str, name: str = "srv-1", status: str = "available") -> dict[str, Any]:
    return {
        "id": raw_id,
        "name": name,
        "status": status,
        "task_state": "completed",
        "task_id": "task-1",
        "key_name": None,
        "domain": None,
        "tags": [],
        "created": "2026-08-24T00:00:00Z",
        "addresses": {
            "public": [
                {"addr": "1.2.3.4", "version": "4", "is_public": True, "type": "ipv4"},
                {"addr": "2001:db8::1", "version": "6", "is_public": True, "type": "ipv6"},
            ]
        },
    }


class TestAuthAndTransport:
    def test_constructor_sets_plain_authorization_header(self) -> None:
        provider = ArvanCloudProvider(api_key=KEY, base_url="https://x/v1", throttle=_no_sleep())
        headers = {k.lower(): v for k, v in provider._client.headers.items()}
        assert headers["authorization"] == KEY
        assert not headers["authorization"].startswith("Bearer")
        assert headers["accept"] == "application/json"
        assert headers["content-type"] == "application/json"

    async def test_key_never_leaks_into_errors(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(401, {"message": "Unauthenticated."})

        provider = _provider(handler)
        with pytest.raises(ProviderAuthError) as exc:
            await provider.get_server(f"{REGION}:srv-1")
        assert KEY not in str(exc.value)
        assert "Unauthenticated" in str(exc.value)

    async def test_region_is_addressed_in_paths(self) -> None:
        paths: list[tuple[str, str]] = []

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            paths.append((method, path))
            return _response(200, [])

        provider = _provider(handler)
        await provider.list_images()
        await provider.list_plans()
        await provider.list_servers()
        assert ("GET", f"/regions/{REGION}/images") in paths
        assert ("GET", f"/regions/{REGION}/sizes") in paths
        assert ("GET", f"/regions/{REGION}/servers") in paths

    async def test_missing_region_fails_for_region_scoped_reads(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(200, []), region="")
        with pytest.raises(ProviderError, match="region"):
            await provider.list_plans()

    async def test_unqualified_server_id_uses_default_region(self) -> None:
        paths: list[tuple[str, str]] = []

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            paths.append((method, path))
            return _response(200, _server_item("abc"))

        provider = _provider(handler)
        server = await provider.get_server("abc")
        assert server is not None
        assert paths == [("GET", f"/regions/{REGION}/servers/abc")]


class TestCatalog:
    async def test_list_locations_maps_region_schema(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            assert path == "/regions"
            return _response(
                200,
                [
                    {
                        "code": "ir-thr-1",
                        "region": "Tehran 1",
                        "country": "IR",
                        "city": "Tehran",
                        "dc": "thr1",
                        "create": True,
                        "beta": False,
                    },
                    {"code": "", "region": "broken"},  # skipped: no code
                ],
            )

        provider = _provider(handler)
        locations = await provider.list_locations()
        assert [loc.id for loc in locations] == ["ir-thr-1"]
        loc = locations[0]
        assert loc.name == "Tehran 1"
        assert loc.country_code == "IR"
        assert loc.city == "Tehran"
        assert loc.metadata["create_allowed"] is True

    async def test_list_locations_falls_back_to_configured_region(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(404, {"message": "Not Found."})

        provider = _provider(handler)
        locations = await provider.list_locations()
        assert [loc.id for loc in locations] == [REGION]
        assert locations[0].metadata["source"] == "configured-default"

    async def test_list_plans_passes_prices_through_metadata(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            assert path == f"/regions/{REGION}/sizes"
            return _response(
                200,
                [
                    {
                        "id": "12",
                        "name": "s1-4c8g",
                        "type": "standard",
                        "cpu_count": 4,
                        "memory_in_bytes": 8 * 1024**3,
                        "disk_in_bytes": 60 * 1024**3,
                        "price_per_hour": 0.55,
                        "price_per_day": 8,
                        "price_per_month": 180,
                    }
                ],
            )

        provider = _provider(handler)
        plans = await provider.list_plans()
        assert len(plans) == 1
        plan = plans[0]
        assert plan.id == "12"
        assert plan.vcpu == 4
        assert plan.memory_mb == 8192
        assert plan.disk_gb == 60
        # §7: provider prices pass through metadata, never hard-coded
        assert plan.metadata["price_per_hour"] == 0.55
        assert plan.metadata["price_per_day"] == 8
        assert plan.metadata["price_per_month"] == 180

    async def test_list_images_maps_imagelist(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(
                200,
                {
                    "images": [
                        {
                            "id": "img-9",
                            "name": "Ubuntu 24.04",
                            "os": "linux",
                            "os_version": "24.04",
                        }
                    ]
                },
            )

        provider = _provider(handler)
        images = await provider.list_images()
        assert len(images) == 1
        assert images[0].id == "img-9"
        assert images[0].os_family == "linux"


class TestServerLifecycle:
    async def test_get_server_returns_qualified_id_and_normalized_status(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            assert path == f"/regions/{REGION}/servers/srv-abc"
            return _response(200, _server_item("srv-abc", status="available"))

        provider = _provider(handler)
        server = await provider.get_server(f"{REGION}:srv-abc")
        assert server is not None
        assert server.id == f"{REGION}:srv-abc"  # region-qualified
        assert server.status == "running"  # available -> running
        assert server.ipv4 == "1.2.3.4"
        assert server.ipv6 == "2001:db8::1"
        assert server.metadata["region"] == REGION
        assert server.metadata["task_state"] == "completed"

    async def test_get_server_not_found_returns_none(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(404, {"message": "server not found"})

        provider = _provider(handler)
        assert await provider.get_server(f"{REGION}:nope") is None

    async def test_list_servers_returns_qualified(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(200, [_server_item("a"), _server_item("b", status="creating")])

        provider = _provider(handler)
        servers = await provider.list_servers()
        assert [s.id for s in servers] == [f"{REGION}:a", f"{REGION}:b"]
        assert [s.status for s in servers] == ["running", "building"]


class TestCreateIdempotency:
    def _create_request(self) -> CreateServerRequest:
        return CreateServerRequest(
            name="srv-00000001",
            plan_id="12",
            image_id="img-9",
            location_id=REGION,
            ssh_key_ids=("key-a",),
            user_data="echo hi",
        )

    async def test_create_posts_mapped_body(self) -> None:
        posted: dict[str, Any] = {}

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            if method == "POST" and path == f"/regions/{REGION}/servers":
                posted.update(kw.get("json", {}))
                return _response(200, _server_item("new-1", name="srv-00000001"))
            # the get-before-create list
            return _response(200, [])

        provider = _provider(handler)
        server = await provider.create_server(self._create_request(), IK)
        assert server.id == f"{REGION}:new-1"
        assert posted["name"] == "srv-00000001"
        assert posted["flavor_id"] == "12"
        assert posted["image_id"] == "img-9"
        assert posted["key_name"] == "key-a"  # single key by NAME (§4)
        assert posted["init_script"] == "echo hi"

    async def test_create_is_idempotent_when_server_already_materialized(self) -> None:
        """A prior attempt that already created the server must be returned
        as-is - no second POST (platform-side idempotency, §6)."""
        calls: list[str] = []

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            calls.append(method)
            if method == "POST":
                pytest.fail("second create must not be sent")
            # get-before-create finds the already-materialized server
            return _response(200, [_server_item("new-1", name="srv-00000001")])

        provider = _provider(handler)
        server = await provider.create_server(self._create_request(), IK)
        assert server.id == f"{REGION}:new-1"
        assert calls == ["GET"]  # correlation read only, no mutation

    async def test_create_requires_region(self) -> None:
        request = self._create_request()
        no_region = CreateServerRequest(
            name=request.name,
            plan_id=request.plan_id,
            image_id=request.image_id,
            location_id="",
        )
        provider = _provider(lambda m, p, **kw: _response(200, []))
        with pytest.raises(ProviderError, match="region"):
            await provider.create_server(no_region, IK)


class TestDeleteAndPower:
    async def test_delete_404_is_success(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            assert method == "DELETE"
            return _response(404, {"message": "server not found"})

        provider = _provider(handler)
        await provider.delete_server(f"{REGION}:gone", IK)  # must not raise

    async def test_delete_posts_delete(self) -> None:
        paths: list[tuple[str, str]] = []

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            paths.append((method, path))
            return _response(200, {})

        provider = _provider(handler)
        await provider.delete_server(f"{REGION}:srv-1", IK)
        assert paths == [("DELETE", f"/regions/{REGION}/servers/srv-1")]

    @pytest.mark.parametrize(
        "method_name, expected_path",
        [
            ("power_on", "/power-on"),
            ("power_off", "/power-off"),
            ("reboot", "/reboot"),
        ],
    )
    async def test_power_calls(self, method_name: str, expected_path: str) -> None:
        paths: list[tuple[str, str]] = []

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            paths.append((method, path))
            return _response(200, {"message": "ok"})

        provider = _provider(handler)
        await getattr(provider, method_name)(f"{REGION}:srv-1", IK)
        assert paths == [("POST", f"/regions/{REGION}/servers/srv-1{expected_path}")]


class TestErrorMapping:
    """PROVIDER_CONTRACT.md §8 - the exact table (metric labels depend on it)."""

    @pytest.mark.parametrize(
        "status,payload,exc_type",
        [
            (401, {"message": "Unauthenticated."}, ProviderAuthError),
            (403, {"message": "Forbidden."}, ProviderAuthError),
            (404, {"message": "not found"}, ProviderNotFound),
            (409, {"message": "already exists"}, ProviderConflict),
            (500, {"message": "boom"}, ProviderUnavailable),
            (503, {"message": "maintenance"}, ProviderUnavailable),
        ],
    )
    async def test_error_table(self, status: int, payload: Any, exc_type: type) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(status, payload)

        provider = _provider(handler)
        with pytest.raises(exc_type):
            await provider.list_images()

    async def test_errors_list_fallback_message(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(400, {"message": "", "errors": [["quota exceeded", "field"]]})

        provider = _provider(handler)
        with pytest.raises(ProviderError, match="quota exceeded"):
            await provider.list_images()

    async def test_empty_error_body_is_defensive(self) -> None:
        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            return _response(500, None, content=b"")

        provider = _provider(handler)
        with pytest.raises(ProviderUnavailable, match="HTTP 500"):
            await provider.list_images()

    async def test_network_error_is_unavailable(self) -> None:
        provider = _provider(
            lambda m, p, **kw: (_ for _ in ()).throw(httpx.ConnectError("refused"))
        )
        with pytest.raises(ProviderUnavailable, match="refused"):
            await provider.list_images()

    async def test_429_with_retry_after_retries_then_succeeds(self) -> None:
        calls = {"n": 0}

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return _response(429, {"message": "slow down"}, headers={"Retry-After": "1"})
            return _response(200, [])

        provider = _provider(handler)
        result = await provider.list_images()
        assert result == []
        assert calls["n"] == 2

    async def test_429_exhausts_retries_into_rate_limited(self) -> None:
        calls = {"n": 0}

        def handler(method: str, path: str, **kw: Any) -> httpx.Response:
            calls["n"] += 1
            return _response(429, {"message": "slow down"}, headers={"Retry-After": "1"})

        provider = _provider(handler)  # max_retries=2
        with pytest.raises(ProviderRateLimited) as exc:
            await provider.list_images()
        assert calls["n"] == 3  # initial + 2 retries
        assert exc.value.reset_at_unix == 1


class TestStatusNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("available", "running"),
            ("AVAILABLE", "running"),
            ("running", "running"),
            ("creating", "building"),
            ("build", "building"),
            ("deleting", "deleting"),
            ("powered-off", "stopped"),
            ("error", "error"),
            ("weird-new-value", "weird-new-value"),  # unknown passes through
        ],
    )
    def test_table(self, raw: str, expected: str) -> None:
        assert normalize_provider_status(raw) == expected


class TestIdHelpers:
    def test_qualify_and_parse_roundtrip(self) -> None:
        qualified = qualify_server_id(REGION, "abc")
        assert parse_server_id(qualified) == (REGION, "abc")

    def test_parse_unqualified_with_default(self) -> None:
        assert parse_server_id("abc", REGION) == (REGION, "abc")

    def test_parse_unqualified_without_default_fails(self) -> None:
        with pytest.raises(ProviderError):
            parse_server_id("abc")


class TestRegistryAndSettings:
    def test_container_registers_arvancloud_when_key_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.core.config import Settings
        from cloud_platform.core.container import Container
        from cloud_platform.providers.registry import ProviderRegistry

        settings = Settings(arvancloud_api_key=KEY, hetzner_api_token="")
        container = Container(
            session_factory=None,  # type: ignore[arg-type]
            provider_registry=ProviderRegistry(),
            provider_allocator=None,  # type: ignore[arg-type]
            hetzner_syncer=None,
        )
        monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: settings)
        monkeypatch.setattr("cloud_platform.core.container.get_settings", lambda: settings)
        container._register_providers()
        assert "arvancloud" in container.provider_registry.keys()
        assert container.provider_registry.get("arvancloud").key == "arvancloud"

    def test_settings_defaults(self) -> None:
        from cloud_platform.core.config import Settings

        s = Settings()
        assert s.arvancloud_api_key == ""
        assert s.arvancloud_api_base_url == "https://napi.arvancloud.ir/ecc/v1"
        assert s.arvancloud_region == ""


class TestSecretLeakage:
    async def test_key_absent_from_all_error_texts(self) -> None:
        """Zero secret leakage: sweep every error path for the raw key."""
        cases = [
            (401, {"message": "Unauthenticated."}, ProviderAuthError),
            (404, {"message": "nf"}, ProviderNotFound),
            (409, {"message": "exists"}, ProviderConflict),
            (500, {"message": "boom"}, ProviderUnavailable),
        ]
        for status, payload, exc_type in cases:

            def handler(
                method: str, path: str, s: int = status, pl: Any = payload, **kw: Any
            ) -> httpx.Response:
                return _response(s, pl)

            provider = _provider(handler)
            with pytest.raises(exc_type) as exc:
                await provider.list_images()
            assert KEY not in str(exc.value), f"key leaked in {exc_type.__name__}"
            assert KEY not in repr(exc.value)
