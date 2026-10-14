"""Sticky management and complete Project usage with synthetic credentials only."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.core.config import Settings
from cloud_platform.providers.hetzner.accounts import (
    HetznerAccountRouter,
    HetznerCredentialAccount,
    build_hetzner_account_router,
)
from cloud_platform.providers.routing import CredentialAccountState, UnknownCredentialAccountError


def account(account_id: str, **kwargs) -> HetznerCredentialAccount:
    return HetznerCredentialAccount(
        account_id=account_id, api_token="synthetic-credential", **kwargs
    )


@pytest.mark.asyncio
async def test_routes_are_sorted_and_draining_account_management_stays_sticky() -> None:
    router = HetznerAccountRouter(
        [
            account("hz-z", priority=20),
            account(" hz-b ", priority=10, state=CredentialAccountState.DRAINING, server_limit=5),
            account("hz-a", priority=10),
            account("hz-disabled", enabled=False),
            account("hz-state-disabled", state=CredentialAccountState.DISABLED),
        ]
    )
    calls: list[tuple[str, str]] = []
    for account_id, provider in router.providers.items():

        def handle(request: httpx.Request, account_id=account_id) -> httpx.Response:
            calls.append((account_id, request.method))
            if request.method == "DELETE":
                return httpx.Response(204)
            if request.url.path.endswith("/servers/123"):
                return httpx.Response(
                    200,
                    json={
                        "server": {
                            "id": 123,
                            "name": "accepted",
                            "status": "off",
                            "server_type": {"name": "cx22"},
                            "image": {"id": 100},
                            "datacenter": {"location": {"name": "fsn1"}},
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {"id": index + 1, "status": "off", "labels": {}} for index in range(5)
                    ],
                    "meta": {"pagination": {"page": 1, "next_page": None}},
                },
            )

        await provider._client.aclose()
        provider._client = httpx.AsyncClient(
            base_url="https://api.hetzner.cloud/v1", transport=httpx.MockTransport(handle)
        )
    try:
        assert [item.account_id for item in router.accounts] == [
            "hz-a",
            "hz-b",
            "hz-z",
            "hz-disabled",
            "hz-state-disabled",
        ]
        assert [item[0] for item in router.new_order_clients()] == ["hz-a", "hz-z"]
        assert set(router.providers) == {"hz-a", "hz-b", "hz-z"}
        assert router.accepts_new_orders("hz-a")
        assert not router.accepts_new_orders("hz-b")
        assert not router.accepts_new_orders("missing")
        usage = await router.server_usage("hz-b")
        assert (
            usage.credential_account_id,
            usage.server_count,
            usage.server_limit,
            usage.full,
        ) == (
            "hz-b",
            5,
            5,
            True,
        )
        managed = router.get_for("hetzner", " hz-b ")
        assert (await managed.get_instance("123")).account_id == "hz-b"
        await managed.delete_instance("123")
        assert calls == [("hz-b", "GET"), ("hz-b", "GET"), ("hz-b", "DELETE")]
        for missing in (None, "default", "missing", "hz-disabled", "hz-state-disabled"):
            with pytest.raises(UnknownCredentialAccountError):
                router.client_for(missing)
            with pytest.raises(UnknownCredentialAccountError):
                router.hourly_for(missing)
        with pytest.raises(KeyError):
            router.get_for("other", "hz-b")
        assert router.views()[0].provider_key == "hetzner"
        assert router.views()[0].key_hint
        assert "synthetic-credential" not in repr(router.views())
        assert "synthetic-credential" not in repr(router.accounts)
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_explicit_default_is_not_aliased_to_preferred_route() -> None:
    router = HetznerAccountRouter(
        [account("preferred", priority=1), account("default", priority=100)]
    )
    try:
        assert router.client_for(None).account_id == "default"
        assert router.hourly_for(None).account_id == "default"
        assert router.client_for("preferred").account_id == "preferred"
    finally:
        await router.aclose()


@pytest.mark.asyncio
async def test_router_closes_owned_providers_once_without_hourly_double_close() -> None:
    router = HetznerAccountRouter([account("a"), account("b")])
    closes: list[AsyncMock] = []
    for provider in router.providers.values():
        close = AsyncMock(wraps=provider.close)
        provider.close = close
        closes.append(close)
    await router.hourly_for("a").aclose()
    await router.aclose()
    await router.aclose()
    assert all(close.await_count == 1 for close in closes)


@pytest.mark.asyncio
async def test_settings_builder_honors_authoritative_explicit_accounts() -> None:
    settings = Settings(
        _env_file=None,
        hetzner_api_token="synthetic-legacy",
        hetzner_accounts=[
            {"id": "b", "api_token": "synthetic-b", "priority": 2, "server_limit": 5},
            {"id": "a", "api_token": "synthetic-a", "priority": 1, "server_limit": 5},
        ],
    )
    router = build_hetzner_account_router(settings)
    assert router is not None
    try:
        assert [account_id for account_id, _ in router.new_order_clients()] == ["a", "b"]
        with pytest.raises(UnknownCredentialAccountError):
            router.client_for("default")
    finally:
        await router.aclose()


def test_duplicate_normalized_accounts_are_rejected_before_building_clients() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        HetznerAccountRouter([account("a"), account(" a ")])
