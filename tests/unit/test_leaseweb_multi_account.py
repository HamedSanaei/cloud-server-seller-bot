"""Leaseweb multi-credential-account regression suite (LEASEWEB-MULTIACCOUNT).

One logical provider key (``leaseweb``) may be served by several API keys, each
with its own Sales Organization scope, transport and throttle. These tests pin
down the properties that make that safe:

- configuration: several accounts, duplicate/invalid ids refused, no enabled
  account refused, and the deprecated single key still working as account
  ``default``;
- isolation: one adapter/transport/holder per account, so a request for one
  account can never carry another's ``X-LSW-Auth`` header;
- discovery: the per-account findings merge into ONE customer-facing catalog,
  with the same (product, location) never duplicated and the fulfillment
  account chosen deterministically;
- failure isolation: a 401/transient failure of one key never destroys the
  catalog or marks other locations unavailable;
- pinning: a checkout freezes ``credential_account_id`` on the server AND the
  provider order, and a missing account fails CLOSED instead of silently
  falling back;
- rotation: rotating one account's credential provably leaves the others
  alone;
- secrets: the fake keys never surface in repr/str/log/CLI output.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.core.config import (
    DEFAULT_CREDENTIAL_ACCOUNT_ID,
    LeasewebAccountSettings,
    Settings,
)
from cloud_platform.modules.checkout.service import MonthlyCheckoutService
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.orders.domain import OrderStatus, ProviderOrder
from cloud_platform.modules.provider_routes.domain import (
    ProviderRoute,
    RouteState,
    select_route_account,
    serving_accounts,
)
from cloud_platform.modules.provider_routes.service import ProviderRouteSelector
from cloud_platform.modules.users.domain import User, UserStatus
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    InsufficientHoldBalanceError,
    Wallet,
)
from cloud_platform.providers.leaseweb.accounts import (
    LeasewebAccountHealth,
    LeasewebAccountRouter,
    LeasewebCredentialAccount,
    LeasewebHealthReport,
    build_leaseweb_account_router,
)
from cloud_platform.providers.leaseweb.ordering import (
    LocationEligibility,
    LocationProbe,
)
from cloud_platform.providers.leaseweb.ordering_sync import (
    AccountCatalogProbe,
    LeaseWebOrderingCatalogSyncer,
)
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.routing import (
    DEFAULT_CREDENTIAL_ACCOUNT,
    CredentialAccountState,
    CredentialAccountView,
    UnknownCredentialAccountError,
    account_state_accepts_new_orders,
    provider_for,
)

#: Recognizable FAKE keys. They exist only in this test file; the whole point is
#: to prove they can never escape into any observable output.
KEY_A = "lsw_test_ACCOUNT_A_SUPER_SECRET"
KEY_B = "lsw_test_ACCOUNT_B_SUPER_SECRET"
KEY_C = "lsw_test_ACCOUNT_C_SUPER_SECRET"

USER_ID = uuid4()
WALLET_ID = uuid4()
OFFER_ID = uuid4()

FRA = "FRA-01"
AMS = "AMS-01"
SIN = "SIN-01"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestAccountConfiguration:
    def test_legacy_single_key_becomes_the_default_account(self) -> None:
        settings = Settings(leaseweb_api_key=KEY_A)
        assert [account.id for account in settings.leaseweb_accounts] == [
            DEFAULT_CREDENTIAL_ACCOUNT
        ]
        assert settings.leaseweb_accounts[0].api_key == KEY_A
        assert settings.leaseweb_configured is True

    def test_two_accounts_initialize_independently(self) -> None:
        settings = Settings(
            leaseweb_accounts=[
                {"id": "lw-eu", "api_key": KEY_A, "priority": 100},
                {"id": "lw-asia", "api_key": KEY_B, "priority": 200},
            ]
        )
        router = build_leaseweb_account_router(settings)
        assert router is not None
        assert router.account_ids == ("lw-eu", "lw-asia")
        assert set(router.providers) == {"lw-eu", "lw-asia"}

    def test_three_accounts_initialize_independently(self) -> None:
        settings = Settings(
            leaseweb_accounts=[
                {"id": "lw-1", "api_key": KEY_A, "priority": 300},
                {"id": "lw-2", "api_key": KEY_B, "priority": 100},
                {"id": "lw-3", "api_key": KEY_C, "priority": 200},
            ]
        )
        router = build_leaseweb_account_router(settings)
        assert router is not None
        assert set(router.account_ids) == {"lw-1", "lw-2", "lw-3"}
        # Deterministic priority order, independent of configuration order.
        assert list(router.ordered_providers) == ["lw-2", "lw-3", "lw-1"]
        assert [account_id for account_id, _ in router.new_order_clients()] == [
            "lw-2",
            "lw-3",
            "lw-1",
        ]

    def test_duplicate_account_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate leaseweb account id"):
            Settings(
                leaseweb_accounts=[
                    {"id": "lw-1", "api_key": KEY_A},
                    {"id": "lw-1", "api_key": KEY_B},
                ]
            )

    def test_invalid_account_id_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="is invalid"):
            Settings(leaseweb_accounts=[{"id": "lw 1/../etc", "api_key": KEY_A}])

    def test_unknown_state_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid state"):
            Settings(leaseweb_accounts=[{"id": "lw-1", "api_key": KEY_A, "state": "retired"}])

    def test_enabled_account_without_api_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="has no api_key"):
            Settings(leaseweb_accounts=[{"id": "lw-1", "api_key": "  "}])

    def test_all_accounts_disabled_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="none is enabled"):
            Settings(
                leaseweb_accounts=[
                    {"id": "lw-1", "api_key": KEY_A, "enabled": False},
                    {"id": "lw-2", "api_key": KEY_B, "enabled": False},
                ]
            )

    def test_draining_account_takes_no_new_orders_but_stays_manageable(self) -> None:
        settings = Settings(
            leaseweb_accounts=[
                {"id": "lw-1", "api_key": KEY_A, "priority": 100, "state": "draining"},
                {"id": "lw-2", "api_key": KEY_B, "priority": 200},
            ]
        )
        assert [account.id for account in settings.leaseweb_new_order_accounts] == ["lw-2"]
        assert {account.id for account in settings.leaseweb_managed_accounts} == {"lw-1", "lw-2"}

    def test_default_account_id_matches_the_provider_neutral_constant(self) -> None:
        assert DEFAULT_CREDENTIAL_ACCOUNT_ID == DEFAULT_CREDENTIAL_ACCOUNT

    def test_settings_accounts_are_neutral_credentials(self) -> None:
        account = LeasewebAccountSettings(id="lw-1", api_key=KEY_A)
        assert account.accepts_new_orders
        assert account.usable
        assert account.has_credential

    def test_toml_keyed_table_uses_the_table_key_as_the_account_id(self) -> None:
        """``[providers.leaseweb.accounts.<id>]`` — the id IS the key, so there
        is nothing to repeat and nothing to drift out of sync."""
        from cloud_platform.core.config import toml_to_settings

        values = toml_to_settings(
            {
                "providers": {
                    "leaseweb": {
                        "enabled": True,
                        "accounts": {
                            "sales-org-north": {"api_key": KEY_A, "label": "North"},
                            "sales-org-south": {"api_key": KEY_B, "enabled": False},
                        },
                    }
                }
            }
        )
        assert values["leaseweb_accounts"] == [
            {"api_key": KEY_A, "label": "North", "id": "sales-org-north"},
            {"api_key": KEY_B, "enabled": False, "id": "sales-org-south"},
        ]

    def test_toml_root_alias_accepts_the_same_keyed_table(self) -> None:
        from cloud_platform.core.config import toml_to_settings

        values = toml_to_settings(
            {"leaseweb": {"accounts": {"sales-org-north": {"api_key": KEY_A}}}}
        )
        assert values["leaseweb_accounts"] == [{"api_key": KEY_A, "id": "sales-org-north"}]

    def test_keyed_accounts_load_into_settings_with_their_labels(self) -> None:
        from cloud_platform.core.config import toml_to_settings

        values = toml_to_settings(
            {
                "leaseweb": {
                    "accounts": {
                        "sales-org-north": {"api_key": KEY_A, "label": "North Org"},
                    }
                }
            }
        )
        settings = Settings(**values)
        assert [account.id for account in settings.leaseweb_accounts] == ["sales-org-north"]
        assert settings.leaseweb_accounts[0].display_name == "North Org"
        assert settings.leaseweb_new_order_accounts[0].api_key == KEY_A

    def test_an_account_id_that_disagrees_with_its_table_key_is_refused(self) -> None:
        """The id addresses every row the account ever created: a rename would
        orphan them, so it fails closed instead of silently winning."""
        from cloud_platform.core.config import toml_to_settings

        with pytest.raises(ValueError, match="declares id"):
            toml_to_settings(
                {"leaseweb": {"accounts": {"sales-org-north": {"id": "other", "api_key": KEY_A}}}}
            )

    def test_label_defaults_to_the_account_id_and_is_never_a_secret(self) -> None:
        account = LeasewebCredentialAccount(account_id="sales-org-north", api_key=KEY_A)
        assert account.display_name == "sales-org-north"
        assert KEY_A not in repr(account)
        with_label = LeasewebCredentialAccount(
            account_id="sales-org-north", api_key=KEY_A, label="  North  "
        )
        assert with_label.display_name == "North"

    def test_toml_array_of_tables_maps_onto_accounts(self) -> None:
        from cloud_platform.core.config import toml_to_settings

        values = toml_to_settings(
            {
                "providers": {
                    "leaseweb": {
                        "enabled": True,
                        "accounts": [
                            {"id": "lw-eu", "api_key": KEY_A, "priority": 100},
                            {"id": "lw-asia", "api_key": KEY_B, "priority": 200},
                        ],
                    }
                }
            }
        )
        assert [entry["id"] for entry in values["leaseweb_accounts"]] == ["lw-eu", "lw-asia"]
        settings = Settings(
            **{k: v for k, v in values.items() if k != "leaseweb_accounts"},
            leaseweb_accounts=values["leaseweb_accounts"],
        )
        assert settings.leaseweb_accounts[1].priority == 200

    def test_toml_file_selects_multiple_accounts(self, tmp_path: Any) -> None:
        from cloud_platform.core.config import Settings as SettingsCls

        path = tmp_path / "configuration.toml"
        path.write_text(
            """
[providers.leaseweb]
enabled = true
locations = ["FRA-01"]

[[providers.leaseweb.accounts]]
id = "lw-eu"
api_key = "TOML_KEY_A"  # pragma: allowlist secret
priority = 100

[[providers.leaseweb.accounts]]
id = "lw-asia"
api_key = "TOML_KEY_B"  # pragma: allowlist secret
priority = 200
""",
            encoding="utf-8",
        )
        settings = SettingsCls.model_validate_toml(path)
        assert [account.id for account in settings.leaseweb_accounts] == ["lw-eu", "lw-asia"]
        assert settings.leaseweb_locations == "FRA-01"


# ---------------------------------------------------------------------------
# Per-account transports
# ---------------------------------------------------------------------------


class TestPerAccountTransports:
    def _router(self) -> LeasewebAccountRouter:
        return LeasewebAccountRouter(
            [
                LeasewebCredentialAccount("lw-eu", KEY_A, priority=100),
                LeasewebCredentialAccount("lw-asia", KEY_B, priority=200),
            ],
            base_url="https://api.test",
        )

    def test_each_account_has_its_own_transport_and_throttle(self) -> None:
        router = self._router()
        eu = router.client_for("lw-eu")
        asia = router.client_for("lw-asia")
        assert eu is not asia
        assert eu._transport is not asia._transport
        assert eu._transport._client is not asia._transport._client
        assert eu._transport.throttle is not asia._transport.throttle

    async def test_account_a_request_never_uses_account_b_key(self) -> None:
        router = self._router()
        eu_headers = await router.client_for("lw-eu")._transport._request_headers({})
        asia_headers = await router.client_for("lw-asia")._transport._request_headers({})
        assert eu_headers["X-LSW-Auth"] == KEY_A
        assert asia_headers["X-LSW-Auth"] == KEY_B
        assert KEY_B not in eu_headers.values()
        assert KEY_A not in asia_headers.values()

    def test_rotating_one_holder_does_not_touch_the_other(self) -> None:
        router = self._router()
        holders = router.credential_holders
        assert holders["lw-eu"].key_hint != holders["lw-asia"].key_hint

    def test_each_account_keeps_its_own_known_secret_set(self) -> None:
        router = self._router()
        assert router.client_for("lw-eu")._transport._known_secrets == {KEY_A}
        assert router.client_for("lw-asia")._transport._known_secrets == {KEY_B}

    def test_unknown_account_fails_closed(self) -> None:
        router = self._router()
        with pytest.raises(UnknownCredentialAccountError) as excinfo:
            router.client_for("lw-missing")
        assert "lw-missing" in str(excinfo.value)
        assert KEY_A not in str(excinfo.value)
        assert KEY_B not in str(excinfo.value)

    def test_disabled_account_is_not_routable(self) -> None:
        router = LeasewebAccountRouter(
            [
                LeasewebCredentialAccount("lw-eu", KEY_A),
                LeasewebCredentialAccount("lw-off", KEY_B, enabled=False),
            ]
        )
        assert router.has_account("lw-off")
        assert not router.has_provider("lw-off")
        with pytest.raises(UnknownCredentialAccountError):
            router.client_for("lw-off")


class TestRegistryCredentialAccounts:
    def _registry(self) -> tuple[ProviderRegistry, Any, Any]:
        registry = ProviderRegistry()
        first = type("P", (), {"key": "leaseweb"})()
        second = type("P", (), {"key": "leaseweb"})()
        registry.register_route("leaseweb", "lw-eu", first)
        registry.register_route("leaseweb", "lw-asia", second)
        return registry, first, second

    def test_pinned_account_resolves_to_its_own_adapter(self) -> None:
        registry, first, second = self._registry()
        assert registry.get_for("leaseweb", "lw-eu") is first
        assert registry.get_for("leaseweb", "lw-asia") is second

    def test_logical_lookup_returns_the_default_adapter(self) -> None:
        registry, first, _ = self._registry()
        assert registry.get("leaseweb") is first
        assert registry.get_for("leaseweb", None) is first

    def test_reserved_default_alias_keeps_legacy_rows_routable(self) -> None:
        # Migration 0035 backfills pre-multi-account Leaseweb rows to the
        # ``default`` account id. If an operator then names their accounts
        # differently, those legacy rows must not become un-routable.
        registry, first, _ = self._registry()
        assert registry.get_for("leaseweb", DEFAULT_CREDENTIAL_ACCOUNT) is first
        # ...while any OTHER unknown account still fails closed.
        with pytest.raises(UnknownCredentialAccountError):
            registry.get_for("leaseweb", "lw-typo")

    def test_missing_account_fails_closed_never_falls_back(self) -> None:
        registry, _, _ = self._registry()
        with pytest.raises(UnknownCredentialAccountError):
            registry.get_for("leaseweb", "lw-deleted")

    def test_single_credential_provider_ignores_account_ids(self) -> None:
        registry = ProviderRegistry()
        only = type("P", (), {"key": "leaseweb"})()
        registry.register(only)
        assert registry.get_for("leaseweb", "default") is only
        assert registry.get_for("leaseweb", None) is only

    def test_provider_for_helper_preserves_fail_closed(self) -> None:
        registry, first, _ = self._registry()
        assert provider_for(registry, "leaseweb", "lw-eu") is first
        with pytest.raises(UnknownCredentialAccountError):
            provider_for(registry, "leaseweb", "lw-deleted")

    def test_provider_for_helper_supports_registries_without_accounts(self) -> None:
        legacy = type("Legacy", (), {"get": lambda self, key: f"adapter:{key}"})()
        assert provider_for(legacy, "leaseweb", "lw-eu") == "adapter:leaseweb"


# ---------------------------------------------------------------------------
# Route selection
# ---------------------------------------------------------------------------


def _route(
    account_id: str,
    location: str,
    *,
    state: RouteState = RouteState.ELIGIBLE_AVAILABLE,
    priority: int = 100,
    product_ids: tuple[str, ...] = ("VPS02_1",),
    account_state: CredentialAccountState = CredentialAccountState.ACTIVE,
) -> ProviderRoute:
    return ProviderRoute(
        provider_key="leaseweb",
        credential_account_id=account_id,
        location_id=location,
        state=state,
        priority=priority,
        product_ids=product_ids,
        account_state=account_state,
    )


class TestRouteSelection:
    def test_same_location_through_two_accounts_is_one_offer(self) -> None:
        routes = [_route("lw-eu", FRA, priority=200), _route("lw-asia", FRA, priority=100)]
        selected = serving_accounts(routes, location_id=FRA, product_id="VPS02_1")
        assert len(selected) == 2  # both routes retained for failover
        assert select_route_account(routes, location_id=FRA, product_id="VPS02_1") == "lw-asia"

    def test_selection_is_deterministic_by_priority_then_id(self) -> None:
        routes = [_route("lw-b", FRA, priority=100), _route("lw-a", FRA, priority=100)]
        assert select_route_account(routes, location_id=FRA) == "lw-a"

    def test_draining_account_does_not_receive_new_orders(self) -> None:
        routes = [
            _route("lw-draining", FRA, priority=10, account_state=CredentialAccountState.DRAINING),
            _route("lw-active", FRA, priority=20),
        ]
        assert select_route_account(routes, location_id=FRA) == "lw-active"

    def test_ineligible_route_is_not_selected(self) -> None:
        routes = [
            _route("lw-eu", FRA, state=RouteState.INELIGIBLE, priority=10),
            _route("lw-asia", FRA, priority=200),
        ]
        assert select_route_account(routes, location_id=FRA) == "lw-asia"

    def test_transient_route_is_not_used_for_new_orders(self) -> None:
        routes = [
            _route("lw-eu", FRA, state=RouteState.TRANSIENT_UNKNOWN, priority=10),
            _route("lw-asia", FRA, priority=200),
        ]
        # An unproven route never receives a NEW billable order: the proven
        # account wins even though it has a worse priority.
        assert select_route_account(routes, location_id=FRA) == "lw-asia"

    def test_transient_route_is_not_a_definitive_negative(self) -> None:
        route = _route("lw-eu", FRA, state=RouteState.TRANSIENT_UNKNOWN)
        assert route.is_definitive_negative is False
        assert route.is_serving is False

    def test_product_level_evidence_is_honoured(self) -> None:
        routes = [_route("lw-eu", FRA, product_ids=("VPS01_1",)), _route("lw-asia", FRA)]
        assert select_route_account(routes, location_id=FRA, product_id="VPS02_1") == "lw-asia"

    def test_location_with_no_serving_route_selects_nothing(self) -> None:
        routes = [_route("lw-eu", FRA, state=RouteState.INELIGIBLE)]
        assert select_route_account(routes, location_id=FRA) is None

    def test_state_helpers(self) -> None:
        assert account_state_accepts_new_orders(CredentialAccountState.ACTIVE)
        assert not account_state_accepts_new_orders(CredentialAccountState.DRAINING)
        assert not account_state_accepts_new_orders(CredentialAccountState.DISABLED)


class TestProviderRouteSelector:
    async def test_selector_reads_the_durable_routes(self) -> None:
        repo = AsyncMock()
        repo.list_for_location = AsyncMock(return_value=[_route("lw-asia", FRA)])
        repo.account_ids_for_provider = AsyncMock(return_value=["lw-asia"])
        selector = ProviderRouteSelector(repository=repo)
        assert await selector.account_for("leaseweb", FRA, "VPS02_1") == "lw-asia"
        assert await selector.has_any_routes("leaseweb") is True

    async def test_selector_returns_none_for_a_provider_without_routes(self) -> None:
        repo = AsyncMock()
        repo.list_for_location = AsyncMock(return_value=[])
        repo.account_ids_for_provider = AsyncMock(return_value=[])
        selector = ProviderRouteSelector(repository=repo)
        assert await selector.account_for("leaseweb", FRA) is None
        assert await selector.has_any_routes("leaseweb") is False

    async def test_selector_returns_none_when_no_route_serves_the_location(self) -> None:
        repo = AsyncMock()
        repo.list_for_location = AsyncMock(
            return_value=[_route("lw-eu", FRA, state=RouteState.INELIGIBLE)]
        )
        repo.account_ids_for_provider = AsyncMock(return_value=["lw-eu"])
        selector = ProviderRouteSelector(repository=repo)
        assert await selector.account_for("leaseweb", FRA) is None


# ---------------------------------------------------------------------------
# Multi-account catalog sync
# ---------------------------------------------------------------------------


class _Product:
    def __init__(self, pid: str = "VPS02_1", price: int = 999, currency: str = "EUR") -> None:
        self.id = pid
        self.name = "VPS S"
        self.vcpu = 2
        self.ram_gb = 4
        self.disk_gb = 100
        self.traffic = "10 TB"
        #: Sales Organizations bill in different currencies; "" means the
        #: response did not report one (never inferred to EUR).
        self.currency = currency
        self.monthly_price_minor = price
        self.location = None


class _Detail:
    def __init__(self, product: _Product, locations: tuple[str, ...]) -> None:
        self.product = product
        self.available_locations = locations


class _Location:
    def __init__(self, code: str) -> None:
        self.id = code
        self.name = code
        self.country_code = "NL"
        self.city = code


class _AccountProvider:
    """Fake per-account adapter with scripted per-location outcomes."""

    def __init__(
        self,
        *,
        account_id: str,
        serves: tuple[str, ...] = (),
        products: tuple[str, ...] = ("VPS02_1",),
        price: int = 999,
        transient: tuple[str, ...] = (),
        auth_failed: bool = False,
        detail_error: Exception | None = None,
        seeds: tuple[str, ...] | None = None,
        currency: str = "EUR",
    ) -> None:
        self.account_id = account_id
        self.key = "leaseweb"
        self.discovery_seeds = seeds if seeds is not None else (FRA, AMS, SIN)
        self._serves = set(serves)
        self._products = products
        self._price = price
        self._currency = currency
        self._transient = set(transient)
        self._auth_failed = auth_failed
        self._detail_error = detail_error
        self._contract_term = "1_MONTH"
        self._billing_cycle = "1_MONTH"
        self.probed: list[str] = []

    async def list_locations(self) -> list[Any]:
        return [_Location(code) for code in self.discovery_seeds]

    def describe_location(self, code: str) -> Any:
        return _Location(code)

    async def list_products_unscoped(self) -> list[Any]:
        if self._auth_failed:
            from cloud_platform.providers.leaseweb.errors import (
                LeasewebAuthenticationError,
            )

            raise LeasewebAuthenticationError("key rejected")
        return []

    async def probe_location(self, location: str) -> LocationProbe:
        self.probed.append(location)
        if self._auth_failed:
            from cloud_platform.providers.leaseweb.errors import LeasewebAuthenticationError

            raise LeasewebAuthenticationError("key rejected")
        if location in self._transient:
            return LocationProbe(
                location, LocationEligibility.TRANSIENT_UNKNOWN, (), (), "transient"
            )
        if location in self._serves:
            return LocationProbe(
                location,
                LocationEligibility.ELIGIBLE_AVAILABLE,
                tuple(_Product(pid, self._price, self._currency) for pid in self._products),
                (),
                f"{len(self._products)} products",
            )
        # Definitive negative: this account's Sales Organization cannot sell
        # here (or nothing is in stock).
        return LocationProbe(
            location, LocationEligibility.INELIGIBLE_ACCOUNT, (), (), "not eligible"
        )

    async def get_product(self, location_id: str, product_id: str) -> Any:
        # Leaseweb's per-product DETAIL endpoint is documented to fail (HTTP
        # 500) for locations whose LIST endpoint answers normally.
        if self._detail_error is not None:
            raise self._detail_error
        return _Detail(_Product(product_id, self._price, self._currency), (location_id,))

    async def verify_credential(self, candidate: str) -> None:
        if self._auth_failed:
            from cloud_platform.providers.leaseweb.errors import LeasewebAuthenticationError

            raise LeasewebAuthenticationError("key rejected")

    async def close(self) -> None:
        return None


def _fake_repo(**methods: Any) -> AsyncMock:
    repo = AsyncMock()
    repo.upsert = AsyncMock(return_value=1)
    repo.upsert_from_provider = AsyncMock(return_value=MagicMock(id="o1"))
    repo.mark_unavailable = AsyncMock(return_value=0)
    repo.list_all = AsyncMock(return_value=[])
    repo.list_for_provider = AsyncMock(return_value=[])
    repo.upsert_observations = AsyncMock(return_value=0)
    repo.account_ids_for_provider = AsyncMock(return_value=[])
    for name, value in methods.items():
        setattr(repo, name, value)
    return repo


def _patch_sync_repos(
    monkeypatch: pytest.MonkeyPatch,
    *,
    offers: dict[str, Any] | None = None,
    routes: dict[str, Any] | None = None,
    **methods: Any,
) -> dict[str, AsyncMock]:
    locations = _fake_repo()
    offers = _fake_repo(**{k: v for k, v in methods.items() if k in {"list_all"}}, **(offers or {}))
    routes = _fake_repo(**(routes or {}))
    monkeypatch.setattr(
        "cloud_platform.modules.catalog.repository.SqlAlchemyLocationRepository",
        lambda *a, **k: locations,
    )
    monkeypatch.setattr(
        "cloud_platform.providers.leaseweb.ordering_sync.SqlAlchemySellableOfferRepository",
        lambda *a, **k: offers,
    )
    monkeypatch.setattr(
        "cloud_platform.providers.leaseweb.ordering_sync.SqlAlchemyProviderRouteRepository",
        lambda *a, **k: routes,
    )
    return {"locations": locations, "offers": offers, "routes": routes}


def _syncer(
    accounts: dict[str, Any],
    *,
    priorities: dict[str, int] | None = None,
    states: dict[str, CredentialAccountState] | None = None,
) -> LeaseWebOrderingCatalogSyncer:
    return LeaseWebOrderingCatalogSyncer(
        lambda: MagicMock(),
        accounts=accounts,
        account_priorities=priorities,
        account_states=states,
    )


def _offered_pairs(repos: dict[str, AsyncMock]) -> list[tuple[str, str]]:
    calls = repos["offers"].upsert_from_provider.await_args_list
    return [(call.kwargs["product_id"], call.kwargs["location_id"]) for call in calls]


class TestMultiAccountCatalogSync:
    async def test_account_a_discovers_fra_and_b_discovers_ams(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,)),
                "lw-asia": _AccountProvider(account_id="lw-asia", serves=(AMS,)),
            }
        )
        result = await syncer.sync_all()
        assert result["products"].total_upserted == 2, result["products"].errors
        assert set(_offered_pairs(repos)) == {("VPS02_1", FRA), ("VPS02_1", AMS)}

    async def test_union_of_locations_appears_in_the_catalog(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA, AMS)),
                "lw-asia": _AccountProvider(account_id="lw-asia", serves=(SIN,)),
            }
        )
        await syncer.sync_all()
        locations = {location for _, location in _offered_pairs(repos)}
        assert locations == {FRA, AMS, SIN}
        # Every discovered location is persisted through the SAME idempotent
        # upsert keyed by (provider, location), so three accounts probing the
        # same code still yield one logical location row.
        upserted = {
            call.kwargs["record"].location_id
            if "record" in call.kwargs
            else call.args[0].location_id
            for call in repos["locations"].upsert.await_args_list
        }
        assert {FRA, AMS, SIN} <= upserted

    async def test_same_product_and_location_is_deduplicated(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,), price=999),
                "lw-asia": _AccountProvider(account_id="lw-asia", serves=(FRA,), price=1299),
            }
        )
        await syncer.sync_all()
        pairs = _offered_pairs(repos)
        assert pairs.count(("VPS02_1", FRA)) == 1
        # The FIRST account in deterministic order supplies the cost snapshot.
        assert (
            repos["offers"].upsert_from_provider.await_args.kwargs["update"].provider_cost_minor
            == 999
        )

    async def test_provider_cost_disagreement_is_logged_not_hidden(
        self, monkeypatch: Any, caplog: Any
    ) -> None:
        _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,), price=999),
                "lw-asia": _AccountProvider(account_id="lw-asia", serves=(FRA,), price=1299),
            }
        )
        with caplog.at_level(logging.WARNING):
            await syncer.sync_all()
        text = "\n".join(record.getMessage() for record in caplog.records)
        assert "differs across credential accounts" in text
        assert KEY_A not in text and KEY_B not in text

    async def test_routing_observations_are_recorded_per_account(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,)),
                "lw-asia": _AccountProvider(account_id="lw-asia", serves=(SIN,)),
            },
            priorities={"lw-eu": 100, "lw-asia": 200},
        )
        await syncer.sync_all()
        assert repos["routes"].upsert_observations.await_count == 1
        observations = list(repos["routes"].upsert_observations.await_args.kwargs["observations"])
        by_account = {(o.credential_account_id, o.location_id): o for o in observations}
        assert by_account[("lw-eu", FRA)].state is RouteState.ELIGIBLE_AVAILABLE
        assert by_account[("lw-eu", FRA)].product_ids == ("VPS02_1",)
        assert by_account[("lw-eu", AMS)].state is RouteState.INELIGIBLE
        assert by_account[("lw-asia", SIN)].state is RouteState.ELIGIBLE_AVAILABLE

    async def test_operator_pricing_is_never_touched(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer({"lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,))})
        import dataclasses

        await syncer.sync_all()
        # The provider-refresh payload has NO operator-owned fields at all, so a
        # sync can never enable/disable an offer or move a customer price.
        for call in repos["offers"].upsert_from_provider.await_args_list:
            names = {field.name for field in dataclasses.fields(call.kwargs["update"])}
            assert "enabled" not in names
            assert "selling_price_minor" not in names


class TestAggregatedInventory:
    """Every credential's inventory merges into ONE catalog, per location.

    A single API key can serve SEVERAL locations, and several keys may each
    serve their own; the sellable item is always
    ``(provider_account_id, location, product_id)``, so the same product in two
    datacenters is two independent offers — never one collapsed row keyed by
    product id alone.
    """

    async def test_one_credential_serving_many_locations_creates_one_offer_each(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer({"north": _AccountProvider(account_id="north", serves=(FRA, AMS, SIN))})
        result = await syncer.sync_all()
        assert result["products"].total_upserted == 3, result["products"].errors
        assert set(_offered_pairs(repos)) == {
            ("VPS02_1", FRA),
            ("VPS02_1", AMS),
            ("VPS02_1", SIN),
        }
        # All three came from ONE credential: the account dimension is recorded
        # even though the key is the same.
        assert {
            call.kwargs["provider_account_id"]
            for call in repos["offers"].upsert_from_provider.await_args_list
        } == {"north"}

    async def test_the_same_product_in_several_locations_is_several_offers(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer({"north": _AccountProvider(account_id="north", serves=(FRA, AMS))})
        await syncer.sync_all()
        pairs = _offered_pairs(repos)
        assert len(pairs) == 2
        assert len(set(pairs)) == 2

    async def test_two_credentials_with_their_own_locations_merge_into_one_catalog(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "north": _AccountProvider(account_id="north", serves=(FRA, AMS)),
                "south": _AccountProvider(account_id="south", serves=(SIN,)),
            }
        )
        await syncer.sync_all()
        by_account: dict[str, set[str]] = {}
        for call in repos["offers"].upsert_from_provider.await_args_list:
            by_account.setdefault(call.kwargs["provider_account_id"], set()).add(
                call.kwargs["location_id"]
            )
        assert by_account == {"north": {FRA, AMS}, "south": {SIN}}

    async def test_a_detail_500_keeps_the_offer_from_the_list_response(
        self, monkeypatch: Any
    ) -> None:
        from cloud_platform.providers.leaseweb.errors import LeasewebServerError

        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "north": _AccountProvider(
                    account_id="north",
                    serves=(FRA,),
                    detail_error=LeasewebServerError("HTTP 500"),
                )
            }
        )
        result = await syncer.sync_all()
        # The LIST endpoint is the authority for membership: the product stays
        # sellable, on the list row's own spec and price.
        assert _offered_pairs(repos) == [("VPS02_1", FRA)]
        assert repos["offers"].upsert_from_provider.await_args.kwargs["update"].name == "VPS S"
        assert result["products"].total_upserted == 1
        # Recorded as a warning, not as a failure that empties the storefront.
        assert any("detail north/VPS02_1/FRA-01" in error for error in result["products"].errors)
        # The product is never listed for hiding either.
        assert ("VPS02_1", FRA) in repos["offers"].mark_unavailable.await_args.args[1]

    async def test_a_detail_500_never_marks_an_existing_offer_unavailable(
        self, monkeypatch: Any
    ) -> None:
        from cloud_platform.providers.leaseweb.errors import LeasewebServerError

        # The location is resolved (the account serves it) and the product is
        # reported, so the pair may not be hidden merely because enrichment
        # failed.
        repos = _patch_sync_repos(monkeypatch, list_all=AsyncMock(return_value=[]))
        syncer = _syncer(
            {
                "north": _AccountProvider(
                    account_id="north",
                    serves=(FRA,),
                    detail_error=LeasewebServerError("HTTP 500"),
                )
            }
        )
        await syncer.sync_all()
        available = repos["offers"].mark_unavailable.await_args.args[1]
        assert ("VPS02_1", FRA) in available

    async def test_a_detail_403_keeps_the_offer_too(self, monkeypatch: Any) -> None:
        from cloud_platform.providers.leaseweb.errors import LeasewebForbiddenError

        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "north": _AccountProvider(
                    account_id="north",
                    serves=(AMS,),
                    detail_error=LeasewebForbiddenError("forbidden"),
                )
            }
        )
        await syncer.sync_all()
        assert _offered_pairs(repos) == [("VPS02_1", AMS)]

    async def test_a_timeout_on_detail_keeps_the_offer_too(self, monkeypatch: Any) -> None:
        from cloud_platform.providers.leaseweb.errors import LeasewebTimeoutError

        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "north": _AccountProvider(
                    account_id="north",
                    serves=(SIN,),
                    detail_error=LeasewebTimeoutError("timed out"),
                )
            }
        )
        await syncer.sync_all()
        assert _offered_pairs(repos) == [("VPS02_1", SIN)]

    async def test_per_credential_location_counts_are_reported(self, monkeypatch: Any) -> None:
        _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "north": _AccountProvider(account_id="north", serves=(FRA, AMS)),
                "south": _AccountProvider(account_id="south", serves=(SIN,)),
            }
        )
        result = await syncer.sync_all()
        assert result["products"].account_locations == {
            "north": {FRA: 1, AMS: 1},
            "south": {SIN: 1},
        }
        assert result["products"].location_count == 3


class TestPerAccountFailureIsolation:
    async def test_one_account_auth_failure_does_not_kill_the_others(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,)),
                "lw-asia": _AccountProvider(account_id="lw-asia", auth_failed=True),
            }
        )
        result = await syncer.sync_all()
        # The healthy account still produces its offers...
        assert ("VPS02_1", FRA) in _offered_pairs(repos)
        # ...and the failed key is reported, per account.
        assert any("lw-asia" in error for error in result["products"].errors)

    async def test_auth_failure_is_recorded_as_a_route_state(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,)),
                "lw-asia": _AccountProvider(account_id="lw-asia", auth_failed=True),
            }
        )
        await syncer.sync_all()
        observations = list(repos["routes"].upsert_observations.await_args.kwargs["observations"])
        failed = [o for o in observations if o.credential_account_id == "lw-asia"]
        assert failed
        assert all(o.state is RouteState.AUTH_FAILED for o in failed)
        # The healthy account's observations are untouched by the failure.
        assert any(
            o.credential_account_id == "lw-eu" and o.state is RouteState.ELIGIBLE_AVAILABLE
            for o in observations
        )

    async def test_transient_failure_preserves_last_known_offers(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(
            monkeypatch,
            list_all=AsyncMock(
                return_value=[
                    MagicMock(
                        provider_key="leaseweb",
                        product_id="VPS02_1",
                        location_id=FRA,
                        provider_available=True,
                    )
                ]
            ),
        )
        syncer = _syncer(
            {"lw-eu": _AccountProvider(account_id="lw-eu", transient=(FRA,))},
        )
        await syncer.sync_all()
        # A flaky key must not hide anything: the location is NOT resolved, so
        # the offer keeps its previous availability.
        repos["offers"].mark_unavailable.assert_awaited_once()
        preserved = repos["offers"].mark_unavailable.await_args.args[1]
        assert ("VPS02_1", FRA) in preserved

    async def test_definitive_loss_hides_only_that_accounts_location(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_sync_repos(
            monkeypatch,
            list_all=AsyncMock(
                return_value=[
                    MagicMock(
                        provider_key="leaseweb",
                        product_id="VPS02_1",
                        location_id=FRA,
                        provider_available=True,
                    ),
                    MagicMock(
                        provider_key="leaseweb",
                        product_id="VPS02_1",
                        location_id=AMS,
                        provider_available=True,
                    ),
                ]
            ),
        )
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,)),
                "lw-asia": _AccountProvider(account_id="lw-asia", serves=(AMS,)),
            }
        )
        await syncer.sync_all()
        repos["offers"].mark_unavailable.assert_awaited_once()
        available = repos["offers"].mark_unavailable.await_args.args[1]
        assert ("VPS02_1", FRA) in available
        assert ("VPS02_1", AMS) in available

    async def test_no_usable_account_never_touches_availability(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(account_id="lw-eu", auth_failed=True),
                "lw-asia": _AccountProvider(account_id="lw-asia", auth_failed=True),
            }
        )
        result = await syncer.sync_all()
        repos["offers"].mark_unavailable.assert_not_awaited()
        repos["offers"].upsert_from_provider.assert_not_awaited()
        assert any("authentication failed" in error for error in result["products"].errors)

    async def test_exactly_one_provider_adapter_per_configured_account(self) -> None:
        settings = Settings(
            leaseweb_accounts=[
                {"id": "lw-1", "api_key": KEY_A},
                {"id": "lw-2", "api_key": KEY_B},
            ]
        )
        router = build_leaseweb_account_router(settings)
        assert router is not None
        transports = {id(provider._transport) for provider in router.providers.values()}
        assert len(transports) == 2


# ---------------------------------------------------------------------------
# `leaseweb accounts doctor`
# ---------------------------------------------------------------------------


class _DoctorRouter:
    """Minimal router surface the doctor needs (fake providers, no transport)."""

    def __init__(
        self, accounts: list[LeasewebCredentialAccount], providers: dict[str, Any]
    ) -> None:
        self._accounts = {account.account_id: account for account in accounts}
        self._providers = dict(providers)

    @property
    def accounts(self) -> tuple[LeasewebCredentialAccount, ...]:
        return tuple(sorted(self._accounts.values(), key=lambda a: (a.priority, a.account_id)))

    @property
    def locations(self) -> tuple[str, ...]:
        return ()

    def account(self, account_id: str | None) -> LeasewebCredentialAccount:
        return self._accounts[str(account_id)]

    def has_provider(self, account_id: str | None) -> bool:
        return str(account_id) in self._providers

    def client_for(self, account_id: str | None) -> Any:
        return self._providers[str(account_id)]

    async def verify_all(self) -> Any:
        """Mirrors the real router: a disabled account has no adapter to verify."""
        return LeasewebHealthReport(
            tuple(
                LeasewebAccountHealth(
                    account_id,
                    ok=account.enabled,
                    error_class=None if account.enabled else "NotConfigured",
                )
                for account_id, account in sorted(self._accounts.items())
            )
        )


class _DoctorOfferRow:
    def __init__(self, provider_key: str = "leaseweb", available: bool = True) -> None:
        self.provider_key = provider_key
        self.provider_available = available


class TestCredentialDoctor:
    """``leaseweb accounts doctor`` shows what EACH key can really sell.

    The output must make three things obvious: every credential authenticated,
    which locations each one serves and with how many products, and where only
    the optional DETAIL endpoint is unavailable (never a reason to lose an
    offer). No location is special-cased anywhere.
    """

    @staticmethod
    def _patch(monkeypatch: Any, router: _DoctorRouter, rows: list[Any]) -> None:
        import cloud_platform.cli as cli_module

        settings = Settings(
            leaseweb_accounts=[
                {"id": "north", "api_key": KEY_A, "label": "North Org"},
                {"id": "south", "api_key": KEY_B},
            ]
        )
        monkeypatch.setattr(
            cli_module, "_leaseweb_account_router_factory", lambda: lambda _s: router
        )
        monkeypatch.setattr(cli_module, "get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.modules.provider_routes.dependents.missing_credential_account_report",
            AsyncMock(return_value=([], None)),
        )

        class _Offers:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def list_all(self) -> list[Any]:
                return rows

        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            _Offers,
        )

    @staticmethod
    def _router(detail_error: Exception | None) -> _DoctorRouter:
        north = _AccountProvider(
            account_id="north",
            serves=("AAA-01", "BBB-02"),
            products=tuple(f"VPS{i:02d}" for i in range(6)),
            seeds=("AAA-01", "BBB-02"),
            detail_error=detail_error,
        )
        south = _AccountProvider(
            account_id="south",
            serves=("CCC-03",),
            products=tuple(f"VPS{i:02d}" for i in range(6)),
            seeds=("CCC-03",),
        )
        return _DoctorRouter(
            [
                LeasewebCredentialAccount(
                    account_id="north", api_key=KEY_A, priority=100, label="North Org"
                ),
                LeasewebCredentialAccount(account_id="south", api_key=KEY_B, priority=200),
            ],
            {"north": north, "south": south},
        )

    async def test_every_credential_reports_its_own_locations_and_counts(
        self, monkeypatch: Any
    ) -> None:
        from cloud_platform.cli import leaseweb_accounts_doctor

        self._patch(monkeypatch, self._router(None), [_DoctorOfferRow() for _ in range(18)])
        result = await leaseweb_accounts_doctor()

        text = "\n".join(result.lines)
        assert result.ok
        assert "[OK ] credential North Org authenticated" in text
        assert "[OK ] credential south authenticated" in text
        assert "[OK ] AAA-01 products: 6" in text
        assert "[OK ] BBB-02 products: 6" in text
        assert "[OK ] CCC-03 products: 6" in text
        assert "credentials: 2" in text
        assert "locations: 3" in text
        assert "offers: 18" in text
        assert KEY_A not in text and KEY_B not in text

    async def test_a_detail_endpoint_failure_is_a_warning_not_a_loss(
        self, monkeypatch: Any
    ) -> None:
        from cloud_platform.cli import leaseweb_accounts_doctor
        from cloud_platform.providers.leaseweb.errors import LeasewebServerError

        self._patch(
            monkeypatch,
            self._router(LeasewebServerError("HTTP 500")),
            [_DoctorOfferRow() for _ in range(12)],
        )
        result = await leaseweb_accounts_doctor()

        text = "\n".join(result.lines)
        # The credential is still healthy and its products still count; only the
        # optional detail endpoint is reported as unavailable.
        assert "[OK ] credential North Org authenticated" in text
        assert "[OK ] AAA-01 products: 6" in text
        assert "[WARN] detail endpoint unavailable for AAA-01" in text
        assert "[WARN] detail endpoint unavailable for BBB-02" in text
        assert "offers: 12" in text

    async def test_an_inconclusive_auth_probe_is_verified_by_scoped_reads(
        self, monkeypatch: Any
    ) -> None:
        """The production false negative, end to end through the command.

        Both credentials were reported INVALID by the authentication probe while
        their scoped catalogs answered normally (six products per location). The
        scoped reads are the authority, so the command must report them healthy.
        """
        from cloud_platform.cli import leaseweb_accounts_doctor

        router = self._router(None)

        async def _inconclusive() -> Any:
            return LeasewebHealthReport(
                tuple(
                    LeasewebAccountHealth(account_id, ok=False, error_class="AuthenticationError")
                    for account_id in ("north", "south")
                )
            )

        monkeypatch.setattr(router, "verify_all", _inconclusive)
        self._patch(monkeypatch, router, [])
        result = await leaseweb_accounts_doctor()

        text = "\n".join(result.lines)
        assert result.ok is True
        assert "[WARN] credential North Org authentication probe inconclusive" in text
        assert "[OK ] credential North Org authenticated — scoped catalog reads" in text
        assert "[OK ] credential south authenticated — scoped catalog reads" in text
        assert "[OK ] AAA-01 products: 6" in text
        assert "[OK ] CCC-03 products: 6" in text
        assert "[FAIL]" not in text
        assert "2 usable account(s)" in text

    async def test_one_broken_credential_is_degraded_not_unavailable(
        self, monkeypatch: Any
    ) -> None:
        """A failing account must not condemn the ones that still work."""
        from cloud_platform.cli import leaseweb_accounts_doctor

        router = self._router(None)
        broken = _AccountProvider(account_id="north", auth_failed=True, serves=("AAA-01",))

        async def _north_failed() -> Any:
            return LeasewebHealthReport(
                (
                    LeasewebAccountHealth("north", ok=False, error_class="AuthenticationError"),
                    LeasewebAccountHealth("south", ok=True),
                )
            )

        monkeypatch.setattr(router, "verify_all", _north_failed)
        router._providers["north"] = broken
        self._patch(monkeypatch, router, [_DoctorOfferRow() for _ in range(6)])
        result = await leaseweb_accounts_doctor()

        text = "\n".join(result.lines)
        assert result.ok is True
        assert "[FAIL] credential North Org" in text
        assert "[WARN] Leaseweb is DEGRADED — 1/2 credential account(s) usable" in text
        assert "[OK ] credential south authenticated" in text
        assert "[OK ] CCC-03 products: 6" in text
        assert "AAA-01 products" not in text

    async def test_a_disabled_credential_is_reported_and_never_probed(
        self, monkeypatch: Any
    ) -> None:
        from cloud_platform.cli import leaseweb_accounts_doctor

        router = self._router(None)
        router._accounts["south"] = LeasewebCredentialAccount(
            account_id="south", api_key=KEY_B, enabled=False, priority=200
        )
        probes: list[str] = []

        async def _record(_provider: Any, account_id: str, **_kw: Any) -> Any:
            probes.append(account_id)
            return AccountCatalogProbe(account_id=account_id, authenticated=True)

        self._patch(monkeypatch, router, [])
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.probe_account_catalog", _record
        )

        result = await leaseweb_accounts_doctor()

        text = "\n".join(result.lines)
        assert probes == ["north"]
        assert "configured but disabled" in text
        assert "credentials: 1" in text


# ---------------------------------------------------------------------------
# Account pinning at checkout
# ---------------------------------------------------------------------------


class _FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class _FakeOfferRepo:
    def __init__(self, offer: SellableOffer) -> None:
        self._offer = offer

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return self._offer if offer_id == OFFER_ID else None


class _FakeAccountRepo:
    async def get_or_create_active(self, user_id: UUID, provider_key: str) -> Any:
        return type("Account", (), {"id": uuid4()})()


class _FakeWalletRepo:
    def __init__(self, balance: int) -> None:
        self.wallet = Wallet(user_id=USER_ID, id=WALLET_ID, balance=balance, currency="EUR")

    async def get(self, user_id: UUID) -> Wallet | None:
        return self.wallet


class _FakeHoldRepo:
    def __init__(self, wallet: _FakeWalletRepo) -> None:
        self._wallet = wallet
        self.holds: dict[str, Hold] = {}
        self.by_id: dict[UUID, Hold] = {}

    async def create_hold(
        self, wallet_id: UUID, amount: int, currency: str, idempotency_key: str
    ) -> Hold:
        existing = self.holds.get(idempotency_key)
        if existing is not None:
            return existing
        if self._wallet.wallet.balance < amount:
            raise InsufficientHoldBalanceError("insufficient")
        hold = Hold(
            wallet_id=wallet_id,
            amount=amount,
            currency=currency,
            idempotency_key=idempotency_key,
            id=uuid4(),
        )
        self.holds[idempotency_key] = hold
        self.by_id[hold.id] = hold
        return hold

    async def get_by_idempotency(self, wallet_id: UUID, idempotency_key: str) -> Hold | None:
        return self.holds.get(idempotency_key)

    async def release_hold(self, hold_id: UUID) -> Hold | None:
        hold = self.by_id.get(hold_id)
        if hold is None or hold.status is not HoldStatus.CREATED:
            return None
        hold.release()
        return hold


class _FakeServerRepo:
    def __init__(self) -> None:
        self.servers: list[CloudServer] = []
        self.by_key: dict[str, CloudServer] = {}

    async def get_by_idempotency_key(self, idempotency_key: str) -> CloudServer | None:
        return self.by_key.get(idempotency_key)

    async def create(self, server: CloudServer, intent: Any) -> CloudServer:
        self.servers.append(server)
        self.by_key[intent.idempotency_key] = server
        server.created_at = datetime.now(UTC)
        return server


class _FakeOrdersRepo:
    def __init__(self) -> None:
        self.by_server: dict[UUID, Any] = {}
        self.created: list[dict[str, Any]] = []

    async def get_by_server(self, server_id: UUID) -> Any:
        return self.by_server.get(server_id)

    async def create(self, *, server_id: UUID, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        order = type(
            "Order",
            (),
            {"id": uuid4(), "server_id": server_id, "status": OrderStatus.PENDING_SUBMIT, **kwargs},
        )()
        self.by_server[server_id] = order
        return order


class _FakeOperationRepo:
    async def get_or_create(self, *, operation_key: str, **kwargs: Any) -> Any:
        return type("Op", (), {"id": uuid4(), "operation_key": operation_key})()


class _RecordingOrdering:
    """Checkout-facing ordering port that records the account it was resolved for."""

    def __init__(self, account_id: str | None, *, price_minor: int = 999) -> None:
        self.account_id = account_id
        self._price = price_minor
        self.calls = 0

    async def get_product(self, location_id: str, product_id: str) -> Any:
        self.calls += 1
        product = type("Product", (), {"monthly_price_minor": self._price, "currency": "EUR"})()
        return type("Detail", (), {"product": product})()

    def os_name_allowed(self, detail: Any, os_name: str) -> bool:
        return os_name == "Ubuntu 24.04"

    async def place_order(self, request: Any, idempotency_key: Any) -> Any:
        raise AssertionError("checkout must never call the provider")

    async def get_order(self, provider_order_id: str) -> Any:
        raise AssertionError("checkout must never call the provider")


class _RoutingRegistry:
    """Registry double whose routes record which account resolved them."""

    def __init__(self, routing: ProviderRouteSelector) -> None:
        self.routing = routing
        self.providers: dict[str | None, _RecordingOrdering] = {}
        self.requested: list[str | None] = []

    def get(self, key: str) -> Any:
        if key != "leaseweb":
            raise KeyError(key)
        return self.providers.get(None)

    def get_for(self, key: str, credential_account_id: str | None = None) -> Any:
        if key != "leaseweb":
            raise KeyError(key)
        self.requested.append(credential_account_id)
        if credential_account_id not in self.providers:
            raise UnknownCredentialAccountError(key, str(credential_account_id))
        return self.providers[credential_account_id]


def _offer(price: int = 1299, location: str = FRA) -> SellableOffer:
    return SellableOffer(
        id=OFFER_ID,
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id=location,
        name="VPS S",
        vcpu=2,
        ram_gb=4,
        disk_gb=100,
        traffic="10 TB",
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        selling_price_minor=price,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
    )


def _customer() -> User:
    return User(id=USER_ID, username="customer", email="c@t.me", status=UserStatus.ACTIVE)


def _checkout(
    *,
    routes: list[ProviderRoute],
    available_accounts: dict[str | None, _RecordingOrdering],
    offer: SellableOffer | None = None,
) -> tuple[MonthlyCheckoutService, dict[str, Any]]:
    wallet = _FakeWalletRepo(10_000)
    holds = _FakeHoldRepo(wallet)
    servers = _FakeServerRepo()
    orders = _FakeOrdersRepo()
    repo = AsyncMock()
    repo.list_for_location = AsyncMock(return_value=list(routes))
    repo.account_ids_for_provider = AsyncMock(
        return_value=sorted({route.credential_account_id for route in routes})
    )
    registry = _RoutingRegistry(ProviderRouteSelector(repository=repo))
    registry.providers.update(available_accounts)
    service = MonthlyCheckoutService(
        server_repo=servers,
        offers_repo=_FakeOfferRepo(offer or _offer()),
        account_repo=_FakeAccountRepo(),
        wallet_repo=wallet,
        hold_repo=holds,
        orders_repo=orders,
        operation_repo=_FakeOperationRepo(),
        audit_repo=_FakeAuditRepo(),
        provider_registry=registry,
        fulfillment_routes=registry.routing,
    )
    return service, {"servers": servers, "orders": orders, "registry": registry, "holds": holds}


class TestCheckoutAccountPinning:
    async def test_new_checkout_snapshots_the_fulfillment_account(self) -> None:
        service, deps = _checkout(
            routes=[_route("lw-asia", FRA, priority=100)],
            available_accounts={"lw-asia": _RecordingOrdering("lw-asia")},
        )
        await service.create_order(
            user=_customer(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-1"
        )
        server = deps["servers"].servers[0]
        assert server.credential_account_id == "lw-asia"
        assert deps["orders"].created[0]["credential_account_id"] == "lw-asia"
        assert server.billing_model == BILLING_MODEL_PREPAID_MONTHLY
        assert server.state is ServerLifecycleState.REQUESTED

    async def test_pinned_account_is_the_highest_priority_eligible_route(self) -> None:
        service, deps = _checkout(
            routes=[
                _route("lw-eu", FRA, priority=200),
                _route("lw-asia", FRA, priority=100),
            ],
            available_accounts={
                "lw-asia": _RecordingOrdering("lw-asia"),
                "lw-eu": _RecordingOrdering("lw-eu"),
            },
        )
        await service.create_order(
            user=_customer(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-2"
        )
        assert deps["servers"].servers[0].credential_account_id == "lw-asia"
        assert deps["registry"].requested == ["lw-asia"]

    async def test_price_revalidation_uses_the_pinned_accounts_catalog(self) -> None:
        service, deps = _checkout(
            routes=[_route("lw-eu", FRA)],
            available_accounts={
                "lw-eu": _RecordingOrdering("lw-eu", price_minor=5000),
                None: _RecordingOrdering(None),
            },
        )
        # A drifted provider price on the PINNED account must block the order,
        # even though the logical adapter still shows the old price.
        with pytest.raises(Exception, match="price changed"):
            await service.create_order(
                user=_customer(),
                offer_id=OFFER_ID,
                os_name="Ubuntu 24.04",
                idempotency_key="k-3",
            )
        assert deps["servers"].servers == []

    async def test_refuses_when_no_account_serves_the_offer(self) -> None:
        service, _ = _checkout(
            routes=[_route("lw-eu", FRA, state=RouteState.INELIGIBLE)],
            available_accounts={"lw-eu": _RecordingOrdering("lw-eu")},
        )
        with pytest.raises(Exception, match="no provider credential account"):
            await service.create_order(
                user=_customer(),
                offer_id=OFFER_ID,
                os_name="Ubuntu 24.04",
                idempotency_key="k-4",
            )

    async def test_single_credential_deployment_pins_nothing(self) -> None:
        """No routes at all => the legacy logical adapter, and no account pin."""
        wallet = _FakeWalletRepo(10_000)
        server_repo = _FakeServerRepo()
        orders = _FakeOrdersRepo()
        repo = AsyncMock()
        repo.list_for_location = AsyncMock(return_value=[])
        repo.account_ids_for_provider = AsyncMock(return_value=[])
        registry = _RoutingRegistry(ProviderRouteSelector(repository=repo))
        registry.providers[None] = _RecordingOrdering(None)
        service = MonthlyCheckoutService(
            server_repo=server_repo,
            offers_repo=_FakeOfferRepo(_offer()),
            account_repo=_FakeAccountRepo(),
            wallet_repo=wallet,
            hold_repo=_FakeHoldRepo(wallet),
            orders_repo=orders,
            operation_repo=_FakeOperationRepo(),
            audit_repo=_FakeAuditRepo(),
            provider_registry=registry,
            fulfillment_routes=registry.routing,
        )
        await service.create_order(
            user=_customer(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-5"
        )
        assert server_repo.servers[0].credential_account_id is None
        assert orders.created[0]["credential_account_id"] is None

    async def test_checkout_without_a_route_selector_keeps_legacy_behaviour(self) -> None:
        wallet = _FakeWalletRepo(10_000)
        server_repo = _FakeServerRepo()
        registry = _RoutingRegistry(ProviderRouteSelector(repository=AsyncMock()))
        registry.providers[None] = _RecordingOrdering(None)
        service = MonthlyCheckoutService(
            server_repo=server_repo,
            offers_repo=_FakeOfferRepo(_offer()),
            account_repo=_FakeAccountRepo(),
            wallet_repo=wallet,
            hold_repo=_FakeHoldRepo(wallet),
            orders_repo=_FakeOrdersRepo(),
            operation_repo=_FakeOperationRepo(),
            audit_repo=_FakeAuditRepo(),
            provider_registry=registry,
            fulfillment_routes=None,
        )
        await service.create_order(
            user=_customer(), offer_id=OFFER_ID, os_name="Ubuntu 24.04", idempotency_key="k-6"
        )
        assert server_repo.servers[0].credential_account_id is None
        # The credential account is never even consulted without a selector.
        assert set(registry.requested) == {None}


class TestPinnedAccountIsAuthoritative:
    """A pinned account is a durable fact, never re-decided later."""

    def test_order_row_carries_the_pin(self) -> None:
        order = ProviderOrder(
            id=uuid4(),
            server_id=uuid4(),
            operation_key="op-1",
            provider_key="leaseweb",
            credential_account_id="lw-2",
        )
        assert order.credential_account_id == "lw-2"

    def test_legacy_order_without_a_pin_resolves_to_the_default(self) -> None:
        registry = ProviderRegistry()
        only = type("P", (), {"key": "leaseweb"})()
        registry.register_route("leaseweb", "default", only)
        order = ProviderOrder(
            id=uuid4(), server_id=uuid4(), operation_key="op-2", provider_key="leaseweb"
        )
        assert order.credential_account_id is None
        assert provider_for(registry, order.provider_key, order.credential_account_id) is only

    def test_server_keeps_its_original_account_even_if_the_route_changes(self) -> None:
        server = CloudServer(
            id=uuid4(),
            user_id=USER_ID,
            provider_key="leaseweb",
            provider_account_id=uuid4(),
            state=ServerLifecycleState.RUNNING,
            credential_account_id="lw-eu",
        )
        registry = ProviderRegistry()
        eu = type("P", (), {"key": "leaseweb"})()
        asia = type("P", (), {"key": "leaseweb"})()
        registry.register_route("leaseweb", "lw-eu", eu)
        registry.register_route("leaseweb", "lw-asia", asia)
        assert provider_for(registry, server.provider_key, server.credential_account_id) is eu

    def test_a_deleted_account_fails_closed_for_existing_resources(self) -> None:
        server = CloudServer(
            id=uuid4(),
            user_id=USER_ID,
            provider_key="leaseweb",
            provider_account_id=uuid4(),
            state=ServerLifecycleState.RUNNING,
            credential_account_id="lw-removed",
        )
        registry = ProviderRegistry()
        registry.register_route("leaseweb", "lw-eu", type("P", (), {"key": "leaseweb"})())
        with pytest.raises(UnknownCredentialAccountError) as excinfo:
            provider_for(registry, server.provider_key, server.credential_account_id)
        message = str(excinfo.value)
        assert "lw-removed" in message
        assert KEY_A not in message and KEY_B not in message


# ---------------------------------------------------------------------------
# Credential rotation
# ---------------------------------------------------------------------------


class TestCredentialRotationIsolation:
    async def test_rotating_one_account_does_not_affect_the_other(self) -> None:
        from cloud_platform.core.container import _CredentialHolderRegistry
        from cloud_platform.modules.credentials.service import CredentialRotationService

        router = LeasewebAccountRouter(
            [
                LeasewebCredentialAccount("lw-eu", KEY_A),
                LeasewebCredentialAccount("lw-asia", KEY_B),
            ]
        )
        holders = _CredentialHolderRegistry()
        for account_id, holder in router.credential_holders.items():
            holders.register("leaseweb", holder, account_id)
        registry = ProviderRegistry()
        for account_id, provider in router.providers.items():
            adapter = provider
            adapter.verify_credential = AsyncMock(return_value=None)  # type: ignore[method-assign]
            registry.register_route("leaseweb", account_id, adapter)

        service = CredentialRotationService(holders, registry, _FakeAuditRepo())
        before_asia = holders.get_holder("leaseweb", "lw-asia")
        assert before_asia is not None
        asia_hint = before_asia.key_hint

        result = await service.rotate(
            provider_key="leaseweb",
            new_credential_value=KEY_C,
            reason="scheduled rotation",
            actor_type=__import__(
                "cloud_platform.modules.audit.domain", fromlist=["ActorType"]
            ).ActorType.SYSTEM,
            actor_id=None,
            credential_account_id="lw-eu",
        )
        assert result.credential_account_id == "lw-eu"
        eu_holder = holders.get_holder("leaseweb", "lw-eu")
        assert eu_holder is not None
        assert (await eu_holder.get()).value == KEY_C
        assert (await before_asia.get()).value == KEY_B
        assert before_asia.key_hint == asia_hint

    async def test_rotating_an_unknown_account_refuses(self) -> None:
        from cloud_platform.core.container import _CredentialHolderRegistry
        from cloud_platform.modules.credentials.domain import (
            ProviderCredentialNotFoundError,
        )
        from cloud_platform.modules.credentials.service import CredentialRotationService

        holders = _CredentialHolderRegistry()
        service = CredentialRotationService(holders, ProviderRegistry(), _FakeAuditRepo())
        with pytest.raises(ProviderCredentialNotFoundError):
            await service.rotate(
                provider_key="leaseweb",
                new_credential_value=KEY_C,
                reason="rotation",
                actor_type=__import__(
                    "cloud_platform.modules.audit.domain", fromlist=["ActorType"]
                ).ActorType.SYSTEM,
                actor_id=None,
                credential_account_id="lw-missing",
            )


# ---------------------------------------------------------------------------
# Health and secrets
# ---------------------------------------------------------------------------


class TestAccountHealth:
    async def test_one_failing_account_reports_degraded_not_unavailable(self) -> None:
        router = LeasewebAccountRouter(
            [
                LeasewebCredentialAccount("lw-eu", KEY_A, priority=100),
                LeasewebCredentialAccount("lw-asia", KEY_B, priority=200),
            ]
        )
        router.providers["lw-eu"].verify_credential = AsyncMock(return_value=None)  # type: ignore[method-assign]
        from cloud_platform.providers.leaseweb.errors import LeasewebAuthenticationError

        router.providers["lw-asia"].verify_credential = AsyncMock(  # type: ignore[method-assign]
            side_effect=LeasewebAuthenticationError("rejected")
        )
        report = await router.verify_all()
        assert report.status == "degraded"
        assert report.healthy == ("lw-eu",)
        assert report.failed == ("lw-asia",)

    async def test_all_accounts_healthy_reports_ok(self) -> None:
        router = LeasewebAccountRouter([LeasewebCredentialAccount("lw-eu", KEY_A)])
        router.providers["lw-eu"].verify_credential = AsyncMock(return_value=None)  # type: ignore[method-assign]
        report = await router.verify_all()
        assert report.status == "ok"

    async def test_all_accounts_failing_reports_unavailable(self) -> None:
        from cloud_platform.providers.leaseweb.errors import LeasewebAuthenticationError

        router = LeasewebAccountRouter(
            [
                LeasewebCredentialAccount("lw-eu", KEY_A),
                LeasewebCredentialAccount("lw-asia", KEY_B),
            ]
        )
        for provider in router.providers.values():
            provider.verify_credential = AsyncMock(  # type: ignore[method-assign]
                side_effect=LeasewebAuthenticationError("rejected")
            )
        report = await router.verify_all()
        assert report.status == "unavailable"

    async def test_diagnostics_never_raise(self) -> None:
        router = LeasewebAccountRouter([LeasewebCredentialAccount("lw-eu", KEY_A)])
        router.providers["lw-eu"].verify_credential = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("socket exploded")
        )
        report = await router.verify_all()
        assert report.status == "unavailable"
        assert report.accounts[0].error_class == "RuntimeError"


class TestSecretDiscipline:
    def test_reprs_never_contain_a_key(self) -> None:
        account = LeasewebCredentialAccount("lw-eu", KEY_A)
        rendered = f"{account!r} {account!s} {account} {account.view()!r}"
        assert KEY_A not in rendered
        settings_account = LeasewebAccountSettings(id="lw-eu", api_key=KEY_A)
        assert KEY_A not in f"{settings_account!r} {settings_account!s} {settings_account}"

    def test_account_views_carry_only_fingerprints(self) -> None:
        account = LeasewebCredentialAccount("lw-eu", KEY_A)
        view = account.view(key_hint="deadbeefcafe")
        assert view.key_hint == "deadbeefcafe"
        assert KEY_A not in repr(view)
        assert view.ref == "leaseweb/lw-eu"

    def test_router_views_never_leak_a_key(self) -> None:
        router = LeasewebAccountRouter(
            [
                LeasewebCredentialAccount("lw-eu", KEY_A),
                LeasewebCredentialAccount("lw-asia", KEY_B),
            ]
        )
        rendered = repr(router.views())
        assert KEY_A not in rendered and KEY_B not in rendered

    def test_fail_closed_error_never_carries_a_key(self) -> None:
        router = LeasewebAccountRouter([LeasewebCredentialAccount("lw-eu", KEY_A)])
        with pytest.raises(UnknownCredentialAccountError) as excinfo:
            router.client_for("lw-nope")
        assert KEY_A not in str(excinfo.value)
        assert KEY_A not in repr(excinfo.value)

    async def test_construction_validation_never_echoes_the_key(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            LeasewebCredentialAccount("lw-x", "")
        assert "lw-x" in str(excinfo.value)

    def test_settings_repr_never_contains_a_key(self) -> None:
        settings = Settings(leaseweb_accounts=[{"id": "lw-eu", "api_key": KEY_A, "priority": 100}])
        assert KEY_A not in repr(settings.leaseweb_accounts)
        assert KEY_A not in str(settings.leaseweb_accounts[0])

    def test_neutral_views_are_log_safe(self) -> None:
        view = CredentialAccountView(
            provider_key="leaseweb",
            account_id="lw-eu",
            state=CredentialAccountState.ACTIVE,
            key_hint="abc123",
        )
        assert view.enabled_for_new_orders
        assert view.usable

    async def test_accounts_list_prints_no_key(self, capsys: Any, monkeypatch: Any) -> None:
        """The operator inventory shows metadata only — never a credential."""
        import cloud_platform.cli as cli_module
        from cloud_platform.cli import leaseweb_accounts_list
        from cloud_platform.providers.leaseweb.accounts import (
            LeasewebAccountHealth,
            LeasewebHealthReport,
        )

        settings = Settings(
            leaseweb_accounts=[
                {"id": "lw-eu", "api_key": KEY_A, "priority": 100},
                {"id": "lw-asia", "api_key": KEY_B, "priority": 200, "state": "draining"},
            ]
        )
        router = build_leaseweb_account_router(settings)
        assert router is not None
        router.verify_all = AsyncMock(  # type: ignore[method-assign]
            return_value=LeasewebHealthReport(
                (
                    LeasewebAccountHealth("lw-asia", ok=True),
                    LeasewebAccountHealth("lw-eu", ok=True),
                )
            )
        )
        monkeypatch.setattr(
            cli_module, "_leaseweb_account_router_factory", lambda: lambda _settings: router
        )
        routes_repo = AsyncMock()
        routes_repo.list_for_provider = AsyncMock(
            return_value=[_route("lw-eu", FRA), _route("lw-asia", SIN)]
        )
        monkeypatch.setattr(
            "cloud_platform.modules.provider_routes.repository.SqlAlchemyProviderRouteRepository",
            lambda *a, **k: routes_repo,
        )
        monkeypatch.setattr(cli_module, "get_settings", lambda: settings)

        code = await leaseweb_accounts_list()

        out = capsys.readouterr().out
        assert code == 0
        assert KEY_A not in out and KEY_B not in out
        assert "lw-eu" in out and "lw-asia" in out
        assert "draining" in out
        assert FRA in out and SIN in out

    def test_deployment_files_carry_no_provider_keys(self) -> None:
        """Keys live in the server-owned configuration.toml, never in deploy files."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        for relative in ("docker-compose.yml", "deploy/production/docker-compose.yml"):
            text = (root / relative).read_text(encoding="utf-8")
            assert "api_key" not in text
            assert "LEASEWEB_API_KEY" not in text
            assert "ACCOUNTS" not in text

    async def test_discovery_never_places_a_billable_order(self, monkeypatch: Any) -> None:
        _patch_sync_repos(monkeypatch)
        placed: list[str] = []

        class _Watched(_AccountProvider):
            async def place_order(self, request: Any, idempotency_key: Any) -> Any:
                placed.append(self.account_id)
                raise AssertionError("discovery must never place an order")

        syncer = _syncer(
            {
                "lw-eu": _Watched(account_id="lw-eu", serves=(FRA,)),
                "lw-asia": _Watched(account_id="lw-asia", serves=(AMS,)),
            }
        )
        await syncer.sync_all()
        assert placed == []


# ---------------------------------------------------------------------------
# Order worker routing (the pinned account is authoritative)
# ---------------------------------------------------------------------------


def _routed_worker(
    *,
    eu_ordering: Any,
    asia_ordering: Any,
    order: ProviderOrder,
    server: CloudServer,
) -> tuple[Any, dict[str, Any]]:
    """Build a real OrderWorker over a REAL two-account provider registry."""
    from test_leaseweb_order_worker import (  # type: ignore[import-not-found]
        FakeAuditRepo,
        FakeHoldRepo,
        FakeHoldService,
        FakeLedgerRepo,
        FakeOfferRepo,
        FakeOperationRepo,
        FakeOrdersRepo,
        FakeRenewalRepo,
        FakeServerRepo,
        FakeWalletRepo,
        _operation,
        _RecordingNotifier,
    )

    from cloud_platform.modules.orders.service import OrderWorker

    registry = ProviderRegistry()
    registry.register_route("leaseweb", "lw-eu", eu_ordering)
    registry.register_route("leaseweb", "lw-asia", asia_ordering)

    op_repo = FakeOperationRepo(_operation())
    orders_repo = FakeOrdersRepo(order)
    holds = FakeHoldRepo()
    ledger = FakeLedgerRepo()
    notifier = _RecordingNotifier()
    # The worker's own offer fixture is keyed to the worker module's OFFER_ID,
    # which the pinned order also uses, so the offer lookup resolves and the
    # pre-POST revalidation runs for real.
    worker = OrderWorker(
        server_repo=FakeServerRepo(server),
        offers_repo=FakeOfferRepo(_offer()),
        orders_repo=orders_repo,
        operation_repo=op_repo,
        wallet_repo=FakeWalletRepo(),
        hold_repo=holds,
        hold_service=FakeHoldService(holds, ledger),
        ledger_repo=ledger,
        audit_repo=FakeAuditRepo(),
        provider_registry=registry,
        renewal_repo=FakeRenewalRepo(),
        delivery_notifier=notifier,
    )
    return worker, {"registry": registry, "orders": orders_repo, "op_repo": op_repo}


def _pinned_fixtures(account_id: str | None) -> tuple[ProviderOrder, CloudServer]:
    from test_leaseweb_order_worker import (  # type: ignore[import-not-found]
        OFFER_ID as WORKER_OFFER_ID,
    )
    from test_leaseweb_order_worker import (
        OP_KEY,
        ORDER_ID,
        SERVER_ID,
    )

    order = ProviderOrder(
        id=ORDER_ID,
        server_id=SERVER_ID,
        operation_key=OP_KEY,
        provider_key="leaseweb",
        offer_id=WORKER_OFFER_ID,
        status=OrderStatus.PENDING_SUBMIT,
        product_id="VPS02_1",
        location_id=FRA,
        os_name="Ubuntu 24.04",
        provider_cost_minor=999,
        provider_cost_currency="EUR",
        credential_account_id=account_id,
    )
    server = CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="leaseweb",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.REQUESTED,
        billing_model=BILLING_MODEL_PREPAID_MONTHLY,
        os="Ubuntu 24.04",
        credential_account_id=account_id,
    )
    return order, server


class TestOrderWorkerAccountRouting:
    async def test_worker_posts_through_the_pinned_account(self) -> None:
        from test_leaseweb_order_worker import (
            FakeOrderingProvider,  # type: ignore[import-not-found]
        )

        order, server = _pinned_fixtures("lw-asia")
        eu = FakeOrderingProvider()
        asia = FakeOrderingProvider()
        worker, _ = _routed_worker(eu_ordering=eu, asia_ordering=asia, order=order, server=server)

        await worker.process_pending(limit=5)

        assert len(asia.posts) == 1, "the pinned account must place the order"
        assert eu.posts == [], "the other account must never be used"

    async def test_worker_uses_the_orders_pin_over_the_servers(self) -> None:
        from test_leaseweb_order_worker import (
            FakeOrderingProvider,  # type: ignore[import-not-found]
        )

        order, server = _pinned_fixtures("lw-asia")
        server.credential_account_id = "lw-eu"  # stale/incorrect server pin
        eu = FakeOrderingProvider()
        asia = FakeOrderingProvider()
        worker, _ = _routed_worker(eu_ordering=eu, asia_ordering=asia, order=order, server=server)
        await worker.process_pending(limit=5)
        assert len(asia.posts) == 1
        assert eu.posts == []

    async def test_legacy_order_without_a_pin_uses_the_logical_adapter(self) -> None:
        from test_leaseweb_order_worker import (
            FakeOrderingProvider,  # type: ignore[import-not-found]
        )

        order, server = _pinned_fixtures(None)
        eu = FakeOrderingProvider()
        asia = FakeOrderingProvider()
        worker, _ = _routed_worker(eu_ordering=eu, asia_ordering=asia, order=order, server=server)
        await worker.process_pending(limit=5)
        # lw-eu registered first => the logical default adapter.
        assert len(eu.posts) == 1
        assert asia.posts == []

    async def test_missing_pinned_account_fails_closed_without_any_post(self) -> None:
        from test_leaseweb_order_worker import (
            FakeOrderingProvider,  # type: ignore[import-not-found]
        )

        order, server = _pinned_fixtures("lw-removed")
        eu = FakeOrderingProvider()
        asia = FakeOrderingProvider()
        worker, _ = _routed_worker(eu_ordering=eu, asia_ordering=asia, order=order, server=server)

        await worker.process_pending(limit=5)

        assert eu.posts == [] and asia.posts == [], "must never fall back to another key"
        assert order.status is OrderStatus.FAILED
        assert "lw-removed" in (order.error or "")
        assert KEY_A not in (order.error or "") and KEY_B not in (order.error or "")

    async def test_outcome_unknown_is_never_re_posts_through_another_account(self) -> None:
        from test_leaseweb_order_worker import (
            FakeOrderingProvider,  # type: ignore[import-not-found]
        )

        order, server = _pinned_fixtures("lw-eu")
        order.status = OrderStatus.OUTCOME_UNKNOWN
        order.provider_order_id = "LS-AMBIGUOUS-1"
        eu = FakeOrderingProvider()
        asia = FakeOrderingProvider()
        worker, _ = _routed_worker(eu_ordering=eu, asia_ordering=asia, order=order, server=server)
        await worker.process_pending(limit=5)
        assert eu.posts == [] and asia.posts == []

    def test_every_resource_scoped_lookup_uses_the_pinned_account(self) -> None:
        """A regression guard: no service may look a server up account-agnostically.

        Order submission, reconciliation, recovery, activation and every
        management capability must resolve the adapter from the resource's
        PINNED account. Re-introducing a bare ``registry.get(server.provider_key)``
        would silently address a customer's server with the wrong credential.
        """
        import inspect
        import re

        from cloud_platform.modules.networking import service as networking_service
        from cloud_platform.modules.operations import service as operations_service
        from cloud_platform.modules.orders import service as orders_service
        from cloud_platform.modules.servers import service as servers_service

        # Whitespace-insensitive, so reformatting the call cannot defeat the guard.
        pinned = re.compile(
            r"provider_for\(\s*self\._registry,\s*server\.provider_key,\s*"
            r"server\.credential_account_id,?\s*\)"
        )
        agnostic = re.compile(r"self\._registry\.get\(\s*server\.provider_key\s*\)")
        for module in (
            orders_service,
            servers_service,
            operations_service,
            networking_service,
        ):
            source = inspect.getsource(module)
            assert pinned.search(source), f"{module.__name__} lost the pinned-account lookup"
            assert not agnostic.search(source), (
                f"{module.__name__} reintroduced an account-agnostic lookup"
            )

    def test_order_worker_prefers_the_order_pin_over_the_server_pin(self) -> None:
        import inspect

        from cloud_platform.modules.orders import service as orders_service

        source = inspect.getsource(orders_service.OrderWorker._process_server)
        squashed = " ".join(source.split())
        assert "order.credential_account_id or server.credential_account_id" in squashed
        assert "UnknownCredentialAccountError" in source


#: A non-German location used to prove currency follows the provider's data,
#: never the location and never a hard-coded default.
LON = "LON-01"


def _offered_updates(repos: dict[str, AsyncMock]) -> list[Any]:
    return [call.kwargs["update"] for call in repos["offers"].upsert_from_provider.await_args_list]


class TestCatalogPersistenceIsReportedHonestly:
    """A run that READ the provider but failed to WRITE is not a successful sync.

    Production evidence: the sync reported products for every scoped location
    while every offer upsert failed with ``relation "provider_routes" does not
    exist``. The command must fail loudly instead, and a failed persistence
    phase must never retire offers it could not even read the state of.
    """

    def _fail(self, message: str) -> AsyncMock:
        return AsyncMock(side_effect=RuntimeError(message))

    async def test_an_offer_upsert_failure_fails_the_run(self, monkeypatch: Any) -> None:
        _patch_sync_repos(
            monkeypatch,
            offers={
                "upsert_from_provider": self._fail('relation "provider_routes" does not exist')
            },
        )
        syncer = _syncer({"lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,))})
        result = await syncer.sync_all()
        products = result["products"]
        assert products.persistence_ok is False
        assert products.offers_failed == 1
        assert products.offers_persisted == 0
        assert products.offer_persistence_failures
        assert products.total_fetched == 1

    async def test_a_route_write_failure_fails_the_run(self, monkeypatch: Any) -> None:
        _patch_sync_repos(
            monkeypatch,
            routes={"upsert_observations": self._fail("provider_routes is missing")},
        )
        syncer = _syncer({"lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,))})
        result = await syncer.sync_all()
        products = result["products"]
        assert products.persistence_ok is False
        assert products.routes_persisted == 0
        assert products.route_persistence_failures
        # Offers themselves were persisted: the run is still not successful,
        # because checkout cannot pin a fulfillment account without routes.
        assert products.offers_persisted == 1

    async def test_a_failed_persistence_phase_never_retires_offers(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(
            monkeypatch,
            offers={"upsert_from_provider": self._fail("schema is behind the image")},
            list_all=AsyncMock(
                return_value=[
                    MagicMock(
                        provider_key="leaseweb",
                        product_id="VPS02_1",
                        location_id=FRA,
                        provider_available=True,
                    )
                ]
            ),
        )
        syncer = _syncer({"lw-eu": _AccountProvider(account_id="lw-eu", serves=(AMS,))})
        result = await syncer.sync_all()
        repos["offers"].mark_unavailable.assert_not_awaited()
        assert result["products"].availability_reconciled is False
        assert any("skipped mark_unavailable" in warning for warning in result["products"].warnings)

    async def test_a_successful_sync_persists_routes_and_every_location_product(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_sync_repos(monkeypatch)
        products = tuple(f"VPS0{index}_1" for index in range(1, 7))
        syncer = _syncer(
            {"lw-eu": _AccountProvider(account_id="lw-eu", products=products, serves=(FRA, LON))}
        )
        result = await syncer.sync_all()
        step = result["products"]
        assert step.persistence_ok is True
        assert step.total_fetched == 12
        assert step.offers_persisted == 12
        # One routing observation per (credential account, location, product).
        assert step.routes_persisted == 12
        assert set(_offered_pairs(repos)) == {
            (pid, location) for pid in products for location in (FRA, LON)
        }
        observations = repos["routes"].upsert_observations.await_args.kwargs["observations"]
        # Every probed location is recorded (including negative verdicts, so the
        # router never has to rediscover them); the two SERVING ones carry this
        # account's product ids.
        serving = {
            observation.location_id: observation
            for observation in observations
            if observation.location_id in {FRA, LON}
        }
        assert set(serving) == {FRA, LON}
        for observation in serving.values():
            assert observation.credential_account_id == "lw-eu"
            assert set(observation.product_ids) == set(products)

    async def test_a_detail_failure_keeps_every_list_product(self, monkeypatch: Any) -> None:
        """LIST is the catalog; DETAIL is optional enrichment (production: 5xx)."""
        from cloud_platform.providers.leaseweb.errors import LeasewebServerError

        repos = _patch_sync_repos(monkeypatch)
        products = tuple(f"VPS0{index}_1" for index in range(1, 7))
        syncer = _syncer(
            {
                "lw-eu": _AccountProvider(
                    account_id="lw-eu",
                    products=products,
                    serves=(FRA,),
                    detail_error=LeasewebServerError("detail endpoint returned HTTP 500"),
                )
            }
        )
        result = await syncer.sync_all()
        step = result["products"]
        # Six list products DISCOVERED, six persisted offers, one recorded
        # detail warning each — and NOTHING retired, and no persistence
        # failure, because a detail outage is not a catalog problem.
        assert step.total_fetched == 6
        assert step.offers_persisted == 6
        assert {pair[0] for pair in _offered_pairs(repos)} == set(products)
        assert step.persistence_ok is True
        assert step.marked_unavailable == 0
        assert len([error for error in step.errors if error.startswith("detail ")]) == 6
        assert repos["offers"].mark_unavailable.await_count == 1
        assert all(update.provider_available is True for update in _offered_updates(repos))

    async def test_the_command_exits_non_zero_when_persistence_fails(
        self, monkeypatch: Any, capsys: Any
    ) -> None:
        from types import SimpleNamespace

        from cloud_platform import cli

        repos = _patch_sync_repos(
            monkeypatch,
            offers={
                "upsert_from_provider": self._fail('relation "provider_routes" does not exist')
            },
        )
        router = SimpleNamespace(
            account_ids=["lw-eu"],
            ordered_providers={"lw-eu": _AccountProvider(account_id="lw-eu", serves=(FRA,))},
            priorities={"lw-eu": 10},
            account_states={},
            locations=(FRA,),
        )
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            lambda settings: router,
        )
        rc = await cli.leaseweb_sync_offers()
        out = capsys.readouterr().out
        assert rc == 1
        assert "FAIL: the catalog was read from the provider but NOT persisted" in out
        assert "PERSISTENCE FAILURE" in out
        assert "alembic current" in out
        # The storefront was NOT described as ready, and nothing was retired.
        assert "Storefront readiness" not in out
        repos["offers"].mark_unavailable.assert_not_awaited()


class TestCredentialProvenanceIsRecorded:
    """A sync must name the account that SUPPLIED each observation.

    Production context: the 36 Leaseweb offers in the repaired database carry
    the legacy ``default`` provenance that migration 0037 backfilled. A
    successful sync is what replaces it — per location, with the account that
    actually answered for it.
    """

    async def test_every_observation_records_its_supplying_account(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "sales-org-north": _AccountProvider(account_id="sales-org-north", serves=(FRA,)),
                "sales-org-uk": _AccountProvider(
                    account_id="sales-org-uk", serves=(LON,), currency="GBP"
                ),
            }
        )
        await syncer.sync_all()
        pinned = {
            call.kwargs["location_id"]: call.kwargs["provider_account_id"]
            for call in repos["offers"].upsert_from_provider.await_args_list
        }
        assert pinned == {FRA: "sales-org-north", LON: "sales-org-uk"}
        assert "default" not in set(pinned.values())

    async def test_routing_observations_also_name_their_account(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "sales-org-north": _AccountProvider(account_id="sales-org-north", serves=(FRA,)),
                "sales-org-uk": _AccountProvider(
                    account_id="sales-org-uk", serves=(LON,), currency="GBP"
                ),
            }
        )
        await syncer.sync_all()
        observations = repos["routes"].upsert_observations.await_args.kwargs["observations"]
        serving = {
            (observation.credential_account_id, observation.location_id)
            for observation in observations
            if observation.location_id in {FRA, LON}
            and observation.state is RouteState.ELIGIBLE_AVAILABLE
        }
        # Each account is recorded ONLY for the locations it really served: the
        # UK key was probed at FRA-01 too (a discovery seed) and is recorded as
        # ineligible there, never as a supplier.
        assert serving == {("sales-org-north", FRA), ("sales-org-uk", LON)}
        assert all(observation.credential_account_id != "default" for observation in observations)


class TestCurrencyIsProviderEvidence:
    """Currency must come from provider data — never from a hard-coded default.

    Production evidence: UK offers are GBP, yet a list-based sync attempted to
    write EUR for LON-11/LON-12 because the parser defaulted a missing currency
    to EUR. Sales Organizations bill in different currencies; a response that
    omits the currency is NOT evidence of EUR.
    """

    def test_parsed_currency_comes_from_the_response(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import _parse_product

        eur = _parse_product({"id": "VPS02_1", "price": {"total": "4.49", "currency": "EUR"}}, FRA)
        gbp = _parse_product({"id": "VPS02_1", "price": {"total": "4.49", "currency": "GBP"}}, LON)
        assert eur is not None and eur.currency == "EUR"
        assert gbp is not None and gbp.currency == "GBP"

    def test_the_same_product_can_bill_in_two_currencies(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import _parse_product

        german = _parse_product(
            {"id": "VPS02_1", "price": {"total": "4.49", "currency": "EUR"}}, "FRA-01"
        )
        british = _parse_product(
            {"id": "VPS02_1", "price": {"total": "4.49", "currency": "GBP"}}, "LON-11"
        )
        assert german is not None and british is not None
        assert german.currency != british.currency

    def test_a_missing_price_currency_is_not_euro(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import _parse_product

        product = _parse_product({"id": "VPS02_1", "price": {"total": "4.49"}}, LON)
        assert product is not None
        assert product.currency == ""

    def test_a_missing_option_currency_is_not_euro(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import _parse_option

        option = _parse_option({"name": "Ubuntu 24.04", "price": "0.00"})
        assert option.currency == ""
        assert _parse_option({"name": "x", "price": "1.00", "currency": "GBP"}).currency == ("GBP")

    def test_the_price_models_have_no_euro_default(self) -> None:
        from cloud_platform.providers.leaseweb.ordering_api import (
            ProductPrice,
            ProductPriceList,
        )

        assert ProductPrice().currency == ""
        assert ProductPriceList().currency == ""

    async def test_a_currency_less_observation_never_overwrites_a_known_currency(
        self, monkeypatch: Any
    ) -> None:
        """The dangerous case, driven end to end through the sync write path."""
        repos = _patch_sync_repos(
            monkeypatch,
            list_all=AsyncMock(
                return_value=[
                    MagicMock(
                        provider_key="leaseweb",
                        product_id="VPS02_1",
                        location_id=LON,
                        provider_available=True,
                    )
                ]
            ),
        )
        from cloud_platform.providers.leaseweb.errors import LeasewebServerError

        syncer = _syncer(
            {
                "lw-uk": _AccountProvider(
                    account_id="lw-uk",
                    serves=(LON,),
                    currency="",
                    # Production shape: the LIST answered, the DETAIL read hit
                    # HTTP 500, so the list row (no currency) is all we have.
                    detail_error=LeasewebServerError("detail endpoint returned HTTP 500"),
                )
            }
        )
        result = await syncer.sync_all()
        step = result["products"]
        # Nothing was written with an unproven currency...
        repos["offers"].upsert_from_provider.assert_not_awaited()
        assert any("currency not reported" in warning for warning in step.warnings)
        # ...and that is a PROVIDER-DATA advisory, not a persistence failure:
        # the run is still usable, and the location is not retired.
        assert step.persistence_ok is True
        assert step.offers_persisted == 0
        repos["offers"].mark_unavailable.assert_awaited_once()
        assert ("VPS02_1", LON) in repos["offers"].mark_unavailable.await_args.args[1]

    async def test_a_gbp_account_writes_gbp(self, monkeypatch: Any) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {"lw-uk": _AccountProvider(account_id="lw-uk", serves=(LON,), currency="GBP")}
        )
        await syncer.sync_all()
        updates = _offered_updates(repos)
        assert [update.provider_cost_currency for update in updates] == ["GBP"]
        assert [update.provider_cost_minor for update in updates] == [999]

    async def test_two_sales_organizations_keep_their_own_currencies(
        self, monkeypatch: Any
    ) -> None:
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {
                "lw-de": _AccountProvider(account_id="lw-de", serves=(FRA,), currency="EUR"),
                "lw-uk": _AccountProvider(account_id="lw-uk", serves=(LON,), currency="GBP"),
            }
        )
        await syncer.sync_all()
        by_location = {
            call.kwargs["location_id"]: call.kwargs["update"].provider_cost_currency
            for call in repos["offers"].upsert_from_provider.await_args_list
        }
        assert by_location == {FRA: "EUR", LON: "GBP"}

    async def test_the_sync_never_touches_a_selling_price_or_currency(
        self, monkeypatch: Any
    ) -> None:
        """SYNC != PRICE: the offer update carries provider observations only."""
        repos = _patch_sync_repos(monkeypatch)
        syncer = _syncer(
            {"lw-uk": _AccountProvider(account_id="lw-uk", serves=(LON,), currency="GBP")}
        )
        await syncer.sync_all()
        updates = _offered_updates(repos)
        assert updates
        for update in updates:
            fields = {field.name for field in dataclasses.fields(update)}
            assert not {name for name in fields if name.startswith("selling_")}
            assert "enabled" not in fields
