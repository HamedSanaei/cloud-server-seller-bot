"""Hetzner VPS management contracts: every request uses an in-memory transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.credentials import CredentialHolder, credential_from_value
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknown,
)
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.vps_ports import (
    VpsActionAccepted,
    VpsPasswordIssued,
    vps_capabilities_of,
)

BASE = "https://api.hetzner.cloud/v1"


def _provider(handler: Any) -> HetznerCloudProvider:
    provider = HetznerCloudProvider("test-token")
    provider._client = httpx.AsyncClient(
        base_url=BASE,
        headers={"Authorization": "Bearer test-token"},
        transport=httpx.MockTransport(handler),
    )
    return provider


def _server(**changes: Any) -> dict[str, Any]:
    row = {
        "id": 42,
        "name": "old-name",
        "status": "running",
        "location": {"name": "fsn1"},
        "datacenter": None,
        "server_type": {"name": "cx22", "architecture": "x86", "disk": 40},
        "image": {"id": 11, "name": "Ubuntu"},
        "public_net": {"ipv4": {"ip": "192.0.2.3"}, "ipv6": {"ip": "2001:db8::1"}},
    }
    row.update(changes)
    return row


def _page(key: str, values: list[dict[str, Any]], next_page: int | None = None) -> httpx.Response:
    return httpx.Response(200, json={key: values, "meta": {"pagination": {"next_page": next_page}}})


def _action() -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "action": {"id": 10, "status": "running", "resources": [{"type": "server", "id": 42}]}
        },
    )


def test_advertises_only_supported_groups() -> None:
    provider = _provider(lambda request: httpx.Response(500))
    caps = vps_capabilities_of(provider.vps_management)
    assert caps.inventory and caps.power and caps.reinstall
    assert caps.credentials and caps.deletion
    assert not any(
        (
            caps.console,
            caps.iso,
            caps.ips,
            caps.snapshots,
            caps.metrics,
            caps.monitoring,
            caps.notifications,
        )
    )
    assert not hasattr(provider.vps_management, "create_vps_snapshot")
    assert not hasattr(provider.vps_management, "get_console_session")
    assert not hasattr(provider.vps_management, "null_route_vps_ip")
    assert provider.vps_management.supports_customer_delete
    assert not hasattr(provider.vps_management, "list_vps_credentials")


async def test_reset_password_returns_redacted_one_time_provider_credential() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            201,
            json={
                "action": {
                    "id": 10,
                    "status": "running",
                    "resources": [{"type": "server", "id": 42}],
                },
                "root_password": "issued-after-reset",  # pragma: allowlist secret
            },
        )

    provider = _provider(handler)
    try:
        result = await provider.vps_management.reset_vps_password("42")
        assert isinstance(result, VpsActionAccepted)
        assert result.provider_server_id == "42"
        assert result.action == "reset_password"
        assert "issued-after-reset" not in repr(result)
        assert "issued-after-reset" not in str(result)
        assert result.root_password is not None
        assert result.root_password.reveal() == "issued-after-reset"
        assert result.root_password.reveal() is None
        assert len(calls) == 1
        assert calls[0].method == "POST"
        assert calls[0].url.path == "/v1/servers/42/actions/reset_password"
        assert not calls[0].content
    finally:
        await provider.close()


@pytest.mark.parametrize("password", [None, "", "  ", 123, ["not-a-password"]])
async def test_reset_password_rejects_missing_or_invalid_provider_password(password: Any) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = {
            "action": {"id": 10, "resources": [{"type": "server", "id": 42}]},
        }
        if password is not None:
            payload["root_password"] = password
        return httpx.Response(201, json=payload)

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown, match="root password"):
            await provider.vps_management.reset_vps_password("42")
        assert len(calls) == 1
    finally:
        await provider.close()


async def test_reset_password_rejects_response_for_another_server() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            201,
            json={
                "action": {"id": 10, "resources": [{"type": "server", "id": 77}]},
                "root_password": "belongs-to-another-server",  # pragma: allowlist secret
            },
        )

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown, match="server mismatch") as error:
            await provider.vps_management.reset_vps_password("42")
        assert "belongs-to-another-server" not in str(error.value)
        assert len(calls) == 1
    finally:
        await provider.close()


async def test_inventory_maps_location_state_and_ips_and_pages() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("/servers/42"):
            return httpx.Response(200, json={"server": _server()})
        assert request.url.path.endswith("/servers")
        if request.url.params["page"] == "1":
            return _page("servers", [_server()], 2)
        return _page("servers", [_server(id=43, status="off", name="second")])

    provider = _provider(handler)
    try:
        one = await provider.vps_management.get_vps_info("42")
        assert one is not None
        assert (one.id, one.state, one.region, one.reference) == (
            "42",
            "running",
            "fsn1",
            "old-name",
        )
        assert one.metadata["ipv4"] == "192.0.2.3"
        assert one.metadata["ipv6"] == "2001:db8::1"
        rows = await provider.vps_management.list_vps_info()
        assert [(row.id, row.state) for row in rows] == [("42", "running"), ("43", "off")]
        assert len(calls) == 3
    finally:
        await provider.close()


async def test_inventory_not_found_and_identity_mismatch() -> None:
    provider = _provider(
        lambda request: httpx.Response(404, json={"error": {"message": "missing"}})
    )
    try:
        assert await provider.vps_management.get_vps_info("42") is None
    finally:
        await provider.close()
    provider = _provider(lambda request: httpx.Response(200, json={"server": _server(id=99)}))
    try:
        with pytest.raises(ProviderError, match="identity mismatch"):
            await provider.vps_management.get_vps_info("42")
    finally:
        await provider.close()


async def test_rename_uses_documented_put_and_verifies_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"server": _server(name="new-name")})

    provider = _provider(handler)
    try:
        result = await provider.vps_management.rename_vps("42", "new-name")
        assert result.reference == "new-name"
        assert len(seen) == 1
        assert (seen[0].method, seen[0].url.path) == ("PUT", "/v1/servers/42")
        assert json.loads(seen[0].content) == {"name": "new-name"}
        with pytest.raises(ProviderError, match="invalid Hetzner server name"):
            await provider.vps_management.rename_vps("42", "../bad")
        assert len(seen) == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "method,path", [("start_vps", "poweron"), ("stop_vps", "poweroff"), ("reboot_vps", "reboot")]
)
async def test_power_uses_documented_action_once(method: str, path: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _action()

    provider = _provider(handler)
    try:
        accepted = await getattr(provider.vps_management, method)("42")
        assert accepted.provider_server_id == "42"
        assert (seen[0].method, seen[0].url.path) == ("POST", f"/v1/servers/42/actions/{path}")
        assert len(seen) == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_mutations_never_retry_unknown_http_outcome(status: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"error": {"message": "retry?"}})

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown, match="outcome unknown"):
            await provider.vps_management.reboot_vps("42")
        assert len(seen) == 1
    finally:
        await provider.close()


async def test_mutation_transport_failure_is_unknown_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("dropped", request=request)

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.vps_management.start_vps("42")
        assert calls == 1
    finally:
        await provider.close()


async def test_reinstall_lists_compatible_system_images_then_rebuilds() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/servers/42"):
            return httpx.Response(200, json={"server": _server()})
        if request.url.path.endswith("/images"):
            assert request.url.params["type"] == "system"
            return _page(
                "images",
                [
                    {
                        "id": 11,
                        "name": "Ubuntu",
                        "os_flavor": "ubuntu",
                        "architecture": "x86",
                        "type": "system",
                        "status": "available",
                        "min_disk_size": 20,
                    },
                    {"id": 12, "architecture": "arm", "type": "system", "status": "available"},
                    {
                        "id": 13,
                        "architecture": "x86",
                        "type": "system",
                        "status": "available",
                        "min_disk_size": 80,
                    },
                    {"id": 14, "architecture": "x86", "type": "system", "status": "deprecated"},
                ],
            )
        assert request.url.path == "/v1/servers/42/actions/rebuild"
        assert json.loads(request.content) == {"image": "11"}
        return httpx.Response(
            201,
            json={
                "action": {
                    "id": 10,
                    "status": "running",
                    "resources": [{"type": "server", "id": 42}],
                },
                "root_password": "issued-after-rebuild",  # pragma: allowlist secret
            },
        )

    provider = _provider(handler)
    try:
        images = await provider.vps_management.list_vps_reinstall_images("42")
        assert [row.id for row in images] == ["11"]
        with pytest.raises(ProviderError, match="not installable"):
            await provider.vps_management.reinstall_vps("42", "12")
        with pytest.raises(ProviderError, match="market apps"):
            await provider.vps_management.reinstall_vps("42", "11", "unsupported")
        accepted = await provider.vps_management.reinstall_vps("42", "11")
        assert isinstance(accepted, VpsActionAccepted)
        assert isinstance(accepted, VpsPasswordIssued)
        assert accepted.action == "reinstall"
        assert accepted.root_password is not None
        assert "issued-after-rebuild" not in repr(accepted)
        assert "issued-after-rebuild" not in str(accepted)
        assert accepted.root_password.reveal() == "issued-after-rebuild"
        assert accepted.root_password.reveal() is None
        assert [request.method for request in calls].count("POST") == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("password_field", [{}, {"root_password": None}])
async def test_reinstall_accepts_ssh_key_response_without_password(
    password_field: dict[str, Any],
) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(
                201,
                json={
                    "action": {"id": 10, "resources": [{"type": "server", "id": 42}]},
                    **password_field,
                },
            )
        if request.url.path.endswith("/images"):
            return _page(
                "images",
                [{"id": 11, "architecture": "x86", "type": "system", "status": "available"}],
            )
        return httpx.Response(200, json={"server": _server()})

    provider = _provider(handler)
    try:
        result = await provider.vps_management.reinstall_vps("42", "11")
        assert isinstance(result, VpsPasswordIssued)
        assert result.root_password is None
        assert len(posts) == 1
    finally:
        await provider.close()


@pytest.mark.parametrize("password", ["", "   ", 123, {"not": "a-password"}])
async def test_reinstall_rejects_malformed_password_without_reissuing_rebuild(
    password: Any,
) -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(
                201,
                json={
                    "action": {"id": 10, "resources": [{"type": "server", "id": 42}]},
                    "root_password": password,
                },
            )
        if request.url.path.endswith("/images"):
            return _page(
                "images",
                [{"id": 11, "architecture": "x86", "type": "system", "status": "available"}],
            )
        return httpx.Response(200, json={"server": _server()})

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown, match="root password"):
            await provider.vps_management.reinstall_vps("42", "11")
        assert len(posts) == 1
    finally:
        await provider.close()


async def test_reinstall_does_not_retry_rebuild_on_ambiguous_response() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"action": {}})
        if request.url.path.endswith("/images"):
            return _page(
                "images",
                [
                    {
                        "id": 11,
                        "name": "Ubuntu",
                        "architecture": "x86",
                        "type": "system",
                        "status": "available",
                    }
                ],
            )
        return httpx.Response(200, json={"server": _server()})

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown, match="action identity"):
            await provider.vps_management.reinstall_vps("42", "11")
        assert len(posts) == 1
    finally:
        await provider.close()


async def test_reinstall_fails_closed_on_missing_image_catalogue_and_auth_error() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request)
            return _action()
        if request.url.path.endswith("/images"):
            return httpx.Response(403, json={"error": {"message": "denied"}})
        return httpx.Response(200, json={"server": _server()})

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderAuthError):
            await provider.vps_management.reinstall_vps("42", "11")
        assert not posts
    finally:
        await provider.close()


async def test_management_uses_rotating_provider_credential_holder() -> None:
    seen: list[str] = []
    holder = CredentialHolder("before")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return _action()

    provider = HetznerCloudProvider("unused", credential_source=holder)
    provider._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    try:
        await provider.vps_management.start_vps("42")
        await holder.swap(credential_from_value("after"))
        await provider.vps_management.stop_vps("42")
        assert seen == ["Bearer before", "Bearer after"]
    finally:
        await provider.close()


async def test_existing_power_ledger_port_does_not_retry_post() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/v1/servers/42/actions/poweron"
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    provider = _provider(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown):
            await provider.power_on("42", IdempotencyKey("power-operation-42"))
        assert calls == 1
    finally:
        await provider.close()
