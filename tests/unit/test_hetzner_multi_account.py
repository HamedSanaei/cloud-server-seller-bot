"""Capacity-aware Hetzner creation: configuration, ownership and durable attempts."""

from __future__ import annotations

import tomllib
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from cloud_platform.core.config import HetznerAccountSettings, Settings, toml_to_settings
from cloud_platform.modules.provider_routes.domain import ProviderRoute, RouteState
from cloud_platform.modules.provider_routes.service import ProviderRouteSelector
from cloud_platform.providers.base import AccountServerUsage
from cloud_platform.providers.errors import (
    ProviderCapacityError,
    ProviderNotFound,
    ProviderUnavailable,
)
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.routing import CredentialAccountState, UnknownCredentialAccountError


@pytest.mark.parametrize("keyed", [True, False])
def test_account_configuration_preserves_management_and_explicit_authority(keyed: bool) -> None:
    header = (
        "[providers.hetzner.accounts.primary]"
        if keyed
        else "[[providers.hetzner.accounts]]\nid = 'primary'"
    )
    data = tomllib.loads(
        "[providers.hetzner]\napi_token = 'legacy-fixture'\n"
        + header
        + "\napi_token = 'project-fixture'\nstate = 'draining'\nserver_limit = 5\npriority = 10\n"
    )
    settings = Settings(_env_file=None, **toml_to_settings(data))
    assert [account.id for account in settings.hetzner_managed_accounts] == ["primary"]
    assert settings.hetzner_new_order_accounts == []
    assert settings.hetzner_accounts[0].server_limit == 5
    assert settings.hetzner_accounts[0].state == CredentialAccountState.DRAINING
    assert "project-fixture" not in repr(settings.hetzner_accounts[0])
    assert "api_token" not in settings.hetzner_accounts[0].model_dump()


def test_single_token_keeps_default_ownership_without_inventing_a_ceiling() -> None:
    settings = Settings(_env_file=None, hetzner_api_token="legacy-fixture")
    assert [account.id for account in settings.hetzner_new_order_accounts] == ["default"]
    assert settings.hetzner_accounts[0].server_limit is None
    explicit_empty = Settings(
        _env_file=None, hetzner_api_token="legacy-fixture", hetzner_accounts=[]
    )
    assert explicit_empty.hetzner_managed_accounts == []


@pytest.mark.parametrize("limit", [0, -1, True, 2.5, "5"])
def test_numeric_ceiling_requires_a_positive_integer(limit: object) -> None:
    with pytest.raises(ValidationError):
        HetznerAccountSettings(id="primary", api_token="fixture", server_limit=limit)


def test_duplicate_normalized_ids_and_unusable_credentials_fail_closed() -> None:
    with pytest.raises(ValidationError, match="duplicate hetzner account"):
        Settings(
            _env_file=None,
            hetzner_accounts=[
                {"id": "primary", "api_token": "fixture"},
                {"id": " primary ", "api_token": "fixture-two"},
            ],
        )
    for token in ("", "  ", "invalid\nheader"):
        with pytest.raises(ValidationError):
            HetznerAccountSettings(id="primary", api_token=token)


def test_default_resource_identity_is_not_a_priority_alias() -> None:
    registry = ProviderRegistry()
    preferred = object()
    original = object()
    registry.register_route("hetzner", "primary", preferred)
    registry.disable_default_account_fallback("hetzner")
    for pin in (None, "", "default", "removed"):
        with pytest.raises(UnknownCredentialAccountError):
            registry.get_for("hetzner", pin)
    registry.register_route("hetzner", "default", original)
    assert registry.get("hetzner") is preferred
    assert registry.get_for("hetzner", None) is original
    assert registry.get_for("hetzner", "primary") is preferred


def test_other_providers_keep_their_existing_default_policy() -> None:
    registry = ProviderRegistry()
    adapter = object()
    registry.register_route("leaseweb", "primary", adapter)
    assert registry.get_for("leaseweb", "default") is adapter


class UsageReader:
    def __init__(self, usage: dict[str, AccountServerUsage | Exception]) -> None:
        self.usage = usage
        self.reads: list[str] = []
        self.draining: set[str] = set()

    def accepts_new_orders(self, account_id: str) -> bool:
        return account_id in self.usage and account_id not in self.draining

    async def server_usage(self, account_id: str) -> AccountServerUsage:
        self.reads.append(account_id)
        usage = self.usage[account_id]
        if isinstance(usage, Exception):
            raise usage
        return usage


def pool_selector(reader: UsageReader) -> ProviderRouteSelector:
    repository = AsyncMock()
    repository.list_for_location.return_value = [
        ProviderRoute(
            "hetzner",
            account_id,
            "fsn1",
            RouteState.ELIGIBLE_AVAILABLE,
            priority=index,
            product_ids=("cx-test",),
        )
        for index, account_id in enumerate(("a", "b"))
    ]
    return ProviderRouteSelector(repository=repository, usage_readers={"hetzner": reader})


async def test_live_full_account_is_skipped_and_unlimited_does_not_mean_zero() -> None:
    reader = UsageReader(
        {
            "a": AccountServerUsage("a", 5, 5),
            "b": AccountServerUsage("b", 2, 5),
        }
    )
    selector = pool_selector(reader)
    assert await selector.account_for("hetzner", "fsn1", "cx-test") == "b"
    reader.usage["a"] = AccountServerUsage("a", 100, None)
    assert await selector.account_for("hetzner", "fsn1", "cx-test") == "a"
    reader.draining.add("a")
    assert await selector.account_for("hetzner", "fsn1", "cx-test") == "b"


async def test_full_and_unreadable_inventory_have_distinct_customer_semantics() -> None:
    reader = UsageReader(
        {
            "a": AccountServerUsage("a", 5, 5),
            "b": ProviderUnavailable("incomplete Project scan"),
        }
    )
    selector = pool_selector(reader)
    with pytest.raises(ProviderUnavailable):
        await selector.account_for("hetzner", "fsn1", "cx-test")
    reader.usage["b"] = AccountServerUsage("b", 5, 5)
    with pytest.raises(ProviderCapacityError):
        await selector.account_for("hetzner", "fsn1", "cx-test")
    with pytest.raises(ProviderNotFound):
        await selector.account_for("hetzner", "fsn1", "unproven-product")
    reader.usage["b"] = AccountServerUsage("b", 0, 5)
    assert await selector.account_for("hetzner", "fsn1", "cx-test", exclude={"a"}) == "b"
    with pytest.raises(ProviderCapacityError):
        await selector.account_for("hetzner", "fsn1", "cx-test", exclude={"a", "b"})


class CatalogRoutes:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], ProviderRoute] = {}

    async def upsert_observations(
        self,
        *,
        provider_key,
        observations,
        priority_of,
        account_state_of,
    ):
        for observation in observations:
            key = (observation.credential_account_id, observation.location_id)
            old = self.rows.get(key)
            products = observation.product_ids
            if not observation.succeeded and old is not None:
                products = old.product_ids
            self.rows[key] = ProviderRoute(
                provider_key,
                *key,
                observation.state,
                priority=priority_of(key[0]),
                product_ids=products,
                account_state=account_state_of(key[0]),
                last_error_class=observation.error_class,
            )

    async def list_for_provider(self, provider_key: str) -> list[ProviderRoute]:
        return list(self.rows.values())

    async def list_for_location(self, provider_key: str, location_id: str) -> list[ProviderRoute]:
        return sorted(
            (route for route in self.rows.values() if route.location_id == location_id),
            key=lambda route: (route.priority, route.credential_account_id),
        )


class CatalogOffers:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], object] = {}
        self.retirements = 0

    async def upsert_from_provider(
        self,
        *,
        provider_key,
        product_id,
        location_id,
        update,
        provider_account_id=None,
        adopt_legacy_catalog_row=False,
    ):
        self.rows[(product_id, location_id)] = update

    async def mark_unavailable(self, provider_key, available, billing_model=None):
        assert all(len(identity) == 3 for identity in available)
        self.retirements += 1
        return 0


async def catalog_pool(
    *,
    counts: dict[str, int],
    unreadable_catalog: str | None = None,
    no_images: str | None = None,
    currencies: dict[str, str | None] | None = None,
):
    from cloud_platform.providers.hetzner.accounts import build_hetzner_account_router

    settings = Settings(
        _env_file=None,
        hetzner_accounts=[
            {"id": "a", "api_token": "fixture-a", "priority": 1, "server_limit": 5},
            {"id": "b", "api_token": "fixture-b", "priority": 2, "server_limit": 5},
        ],
    )
    router = build_hetzner_account_router(settings)
    assert router is not None
    for account_id, provider in router.providers.items():
        await provider._client.aclose()

        def handle(request: httpx.Request, account_id=account_id):
            assert request.method == "GET"
            assert request.headers["Authorization"] == f"Bearer fixture-{account_id}"
            key = request.url.path.rsplit("/", 1)[-1]
            if key == "pricing":
                currency = currencies[account_id] if currencies is not None else "EUR"
                return httpx.Response(
                    200, json={"pricing": {} if currency is None else {"currency": currency}}
                )
            if key == "server_types" and account_id == unreadable_catalog:
                return httpx.Response(503, json={"error": {"code": "service_unavailable"}})
            if key == "servers":
                rows = [
                    {"id": index + 1, "name": f"manual-{index}", "status": "off"}
                    for index in range(counts[account_id])
                ]
            elif key == "locations":
                rows = [{"id": 1, "name": "fsn1", "country": "DE", "city": "Falkenstein"}]
            elif key == "server_types":
                rows = [
                    {
                        "id": 22,
                        "name": "cx22",
                        "architecture": "x86",
                        "cores": 2,
                        "memory": 4,
                        "disk": 40,
                        "storage_type": "local",
                        "locations": [{"name": "fsn1", "available": True}],
                        "prices": [
                            {
                                "location": "fsn1",
                                "included_traffic": 21990232555520,
                                "price_hourly": {
                                    "gross": "0.0075" if account_id == "a" else "0.0150"
                                },
                                "price_monthly": {"gross": "3.92" if account_id == "a" else "9.99"},
                            }
                        ],
                    }
                ]
            elif key == "images":
                rows = (
                    []
                    if no_images == account_id
                    else [
                        {
                            "id": 100,
                            "name": "ubuntu-24.04",
                            "type": "system",
                            "status": "available",
                            "os_flavor": "ubuntu",
                            "architecture": "x86",
                            "deprecated": False,
                        }
                    ]
                )
            else:
                raise AssertionError(request.url)
            return httpx.Response(
                200,
                json={
                    key: rows,
                    "meta": {"pagination": {"page": 1, "next_page": None}},
                },
            )

        provider._client = httpx.AsyncClient(
            base_url="https://fixture.invalid/v1",
            transport=httpx.MockTransport(handle),
        )
    return router


@pytest.mark.parametrize("billing_model", ["prepaid_monthly_fixed", "hourly"])
async def test_catalog_publishes_one_independent_headroom_observation(
    monkeypatch,
    billing_model,
):
    from cloud_platform.providers.hetzner import sync

    offers, routes = CatalogOffers(), CatalogRoutes()
    monkeypatch.setattr(sync, "SqlAlchemySellableOfferRepository", lambda *a, **k: offers)
    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
        lambda factory: routes,
    )
    router = await catalog_pool(counts={"a": 5, "b": 2})
    try:
        result = await sync.HetznerCatalogSyncer(
            lambda: None,
            account_router=router,
        ).sync_offers(billing_model)
        assert result.verified_accounts == frozenset({("b", "cx22", "fsn1")})
        assert list(offers.rows) == [("cx22", "fsn1")]
        observation = offers.rows[("cx22", "fsn1")]
        assert observation.provider_account_id == "b"
        assert observation.provider_cost_minor == (999 if billing_model != "hourly" else 2)
        if billing_model == "hourly":
            assert observation.billing_parameters["provider_hourly_rate"] == "0.015"
        selector = ProviderRouteSelector(repository=routes, usage_readers={"hetzner": router})
        assert await selector.account_for("hetzner", "fsn1", "cx22") == "b"
    finally:
        await router.aclose()


async def test_full_pool_keeps_catalog_and_unreadable_sibling_is_not_full(monkeypatch):
    from cloud_platform.providers.hetzner import sync

    offers, routes = CatalogOffers(), CatalogRoutes()
    monkeypatch.setattr(sync, "SqlAlchemySellableOfferRepository", lambda *a, **k: offers)
    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
        lambda factory: routes,
    )
    router = await catalog_pool(counts={"a": 5, "b": 5}, unreadable_catalog="b")
    try:
        result = await sync.HetznerCatalogSyncer(
            lambda: None,
            account_router=router,
        ).sync_offers("hourly")
        assert result.verified_accounts == frozenset({("a", "cx22", "fsn1")})
        assert routes.rows[("b", "fsn1")].state == RouteState.TRANSIENT_UNKNOWN
        assert offers.retirements == 0
        selector = ProviderRouteSelector(repository=routes, usage_readers={"hetzner": router})
        with pytest.raises(ProviderUnavailable):
            await selector.account_for("hetzner", "fsn1", "cx22")
    finally:
        await router.aclose()


@pytest.mark.parametrize(
    "sync_order",
    [
        ("prepaid_monthly_fixed", "hourly"),
        ("hourly", "prepaid_monthly_fixed"),
    ],
)
async def test_billing_family_proofs_do_not_replace_shared_product_membership(
    monkeypatch, sync_order
):
    from cloud_platform.providers.hetzner import sync

    offers, routes = CatalogOffers(), CatalogRoutes()
    monkeypatch.setattr(sync, "SqlAlchemySellableOfferRepository", lambda *a, **k: offers)
    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
        lambda factory: routes,
    )
    router = await catalog_pool(counts={"a": 5, "b": 0}, no_images="b")
    try:
        syncer = sync.HetznerCatalogSyncer(lambda: None, account_router=router)
        results = {model: await syncer.sync_offers(model) for model in sync_order}
        assert results["hourly"].verified_accounts == frozenset({("a", "cx22", "fsn1")})
        assert results["prepaid_monthly_fixed"].verified_accounts == frozenset(
            {("b", "cx22", "fsn1")}
        )
        assert routes.rows[("b", "fsn1")].state == RouteState.ELIGIBLE_AVAILABLE
        assert routes.rows[("b", "fsn1")].product_ids == ("cx22",)
        selector = ProviderRouteSelector(repository=routes, usage_readers={"hetzner": router})
        assert await selector.account_for("hetzner", "fsn1", "cx22") == "b"
    finally:
        await router.aclose()


@pytest.mark.parametrize("billing_model", ["prepaid_monthly_fixed", "hourly"])
async def test_catalog_native_cost_and_currency_belong_to_publishing_account(
    monkeypatch,
    billing_model,
):
    from cloud_platform.providers.hetzner import sync

    offers, routes = CatalogOffers(), CatalogRoutes()
    monkeypatch.setattr(sync, "SqlAlchemySellableOfferRepository", lambda *a, **k: offers)
    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
        lambda factory: routes,
    )
    router = await catalog_pool(counts={"a": 5, "b": 0}, currencies={"a": "EUR", "b": "USD"})
    try:
        result = await sync.HetznerCatalogSyncer(lambda: None, account_router=router).sync_offers(
            billing_model
        )
        observation = offers.rows[("cx22", "fsn1")]
        assert observation.provider_account_id == "b"
        assert observation.provider_cost_currency == "USD"
        assert observation.provider_cost_minor == (2 if billing_model == "hourly" else 999)
        assert result.verified_accounts == frozenset({("b", "cx22", "fsn1")})
    finally:
        await router.aclose()


async def test_unreadable_account_currency_keeps_sibling_independently_qualified(monkeypatch):
    from cloud_platform.providers.hetzner import sync

    offers, routes = CatalogOffers(), CatalogRoutes()
    monkeypatch.setattr(sync, "SqlAlchemySellableOfferRepository", lambda *a, **k: offers)
    monkeypatch.setattr(
        "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
        lambda factory: routes,
    )
    router = await catalog_pool(counts={"a": 0, "b": 0}, currencies={"a": None, "b": "GBP"})
    try:
        result = await sync.HetznerCatalogSyncer(lambda: None, account_router=router).sync_offers(
            "hourly"
        )
        assert result.verified_accounts == frozenset({("b", "cx22", "fsn1")})
        assert offers.rows[("cx22", "fsn1")].provider_cost_currency == "GBP"
        assert offers.retirements == 0
        assert not result.availability_reconciled
        assert routes.rows[("a", "fsn1")].product_ids == ("cx22",)
    finally:
        await router.aclose()
