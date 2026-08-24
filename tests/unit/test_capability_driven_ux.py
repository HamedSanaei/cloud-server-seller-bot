"""Tests for capability-driven Telegram UX (M15-005).

Acceptance: unsupported controls hidden; shared flows reused. The server
detail screen derives its controls from the provider's advertised
capabilities via the shared SERVER_CONTROLS table (the same table the
command gate uses), and the purchase flow serves offers from any provider
through the same screens.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

from cloud_platform.modules.catalog.domain import (
    CatalogOffer,
    LocationRecord,
)
from cloud_platform.modules.catalog.service import BuyFlowViewService
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.operations.service import (
    SERVER_CONTROLS,
    PowerControlsService,
    available_power_actions,
    available_server_controls,
)
from cloud_platform.providers.arvancloud.client import ARVANCLOUD_CAPABILITIES
from cloud_platform.providers.base import Capability
from cloud_platform.providers.registry import ProviderRegistry

SIGNING_KEY = "test-signing-key-m15-005"


class TestControlTable:
    def test_every_control_is_capability_and_state_gated(self) -> None:
        for control in SERVER_CONTROLS:
            assert control.capability  # a real Capability
            assert control.action
            assert control.label
            assert control.state_gates  # non-empty gate

    def test_power_controls_cover_exactly_the_power_actions(self) -> None:
        power_controls = [c for c in SERVER_CONTROLS if c.capability is Capability.POWER]
        assert {c.action for c in power_controls} == {
            "power_on",
            "power_off",
            "reboot",
        }

    def test_running_server_shows_off_and_reboot_only(self) -> None:
        controls = available_server_controls(
            ServerLifecycleState.RUNNING, frozenset({Capability.POWER})
        )
        assert [c.action for c in controls] == ["power_off", "reboot"]

    def test_stopped_server_shows_power_on_only(self) -> None:
        controls = available_server_controls(
            ServerLifecycleState.STOPPED, frozenset({Capability.POWER})
        )
        assert [c.action for c in controls] == ["power_on"]

    def test_no_power_capability_hides_every_control(self) -> None:
        controls = available_server_controls(
            ServerLifecycleState.RUNNING, frozenset({Capability.COMPUTE})
        )
        assert controls == []

    def test_provisioning_server_has_no_controls(self) -> None:
        controls = available_server_controls(
            ServerLifecycleState.PROVISIONING, frozenset({Capability.POWER})
        )
        assert controls == []

    def test_table_matches_available_power_actions(self) -> None:
        """The table and the legacy gate never disagree."""
        for state in (ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED):
            for caps in (
                frozenset({Capability.POWER}),
                frozenset(),
                ARVANCLOUD_CAPABILITIES,
            ):
                from_actions = set(available_power_actions(state, caps))
                from_table = {
                    action
                    for action in ("power_on", "power_off", "reboot")
                    if any(c.action == action for c in available_server_controls(state, caps))
                }
                assert {a.value for a in from_actions} == from_table


def _server(state: ServerLifecycleState, provider_key: str = "hetzner") -> CloudServer:
    return CloudServer(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        provider_key=provider_key,
        provider_account_id=uuid.uuid4(),
        provider_server_id=f"{provider_key}:srv-1",
        state=state,
    )


class _FakeServerRepo:
    def __init__(self, server: CloudServer | None) -> None:
        self._server = server
        self.get = AsyncMock(return_value=server)


class TestSharedFlowsAcrossProviders:
    """The SAME detail screen + control table serves Hetzner and ArvanCloud
    servers - no per-provider UI code (shared flows reused)."""

    def _service(self, provider: Any) -> PowerControlsService:
        registry = ProviderRegistry()
        registry.register(provider)
        return PowerControlsService(
            server_repo=_FakeServerRepo(None),  # replaced per test
            provider_registry=registry,
            signing_key=SIGNING_KEY,
        )

    async def test_arvancloud_server_gets_the_shared_power_controls(self) -> None:
        from cloud_platform.providers.arvancloud.client import ArvanCloudProvider, Throttle

        async def wait(_s: float) -> None:
            return None

        provider = ArvanCloudProvider(
            api_key="MU-TEST",
            base_url="https://x/v1",
            region="ir-thr-1",
            throttle=Throttle(max_rps=1000.0, wait=wait),
        )
        server = _server(ServerLifecycleState.RUNNING, provider_key="arvancloud")
        service = self._service(provider)
        service._servers = _FakeServerRepo(server)
        view = await service.detail(server.user_id, server.id)
        assert view is not None
        # the same shared controls Hetzner gets for a running server
        assert [a.action.value for a in view.actions] == ["power_off", "reboot"]
        assert [a.label for a in view.actions] == ["Power off", "Reboot"]

    async def test_hetzner_server_gets_identical_controls(self) -> None:
        class HetznerLike:
            key = "hetzner"
            capabilities = frozenset({Capability.POWER})

        server = _server(ServerLifecycleState.RUNNING, provider_key="hetzner")
        service = self._service(HetznerLike())
        service._servers = _FakeServerRepo(server)
        hetzner_view = await service.detail(server.user_id, server.id)

        from cloud_platform.providers.arvancloud.client import ArvanCloudProvider, Throttle

        async def wait(_s: float) -> None:
            return None

        arvan = ArvanCloudProvider(
            api_key="MU-TEST",
            base_url="https://x/v1",
            region="ir-thr-1",
            throttle=Throttle(max_rps=1000.0, wait=wait),
        )
        arvan_server = _server(ServerLifecycleState.RUNNING, provider_key="arvancloud")
        service2 = self._service(arvan)
        service2._servers = _FakeServerRepo(arvan_server)
        arvan_view = await service2.detail(arvan_server.user_id, arvan_server.id)

        assert hetzner_view is not None
        assert arvan_view is not None
        assert [a.action.value for a in hetzner_view.actions] == [
            a.action.value for a in arvan_view.actions
        ]
        assert [a.label for a in hetzner_view.actions] == [a.label for a in arvan_view.actions]

    async def test_provider_without_power_hides_all_controls(self) -> None:
        class ReadOnlyProvider:
            key = "readonly"
            capabilities = frozenset({Capability.COMPUTE})

        server = _server(ServerLifecycleState.RUNNING, provider_key="readonly")
        service = self._service(ReadOnlyProvider())
        service._servers = _FakeServerRepo(server)
        view = await service.detail(server.user_id, server.id)
        assert view is not None
        assert view.actions == ()  # hidden, not shown disabled

    async def test_arvancloud_full_capability_set_does_not_add_unknown_controls(self) -> None:
        """ArvanCloud advertises SNAPSHOT/FIREWALL/... but those flows do not
        exist yet: the table has no rows for them, so nothing phantom is
        rendered - only the implemented shared flows appear."""
        from cloud_platform.providers.arvancloud.client import ArvanCloudProvider, Throttle

        async def wait(_s: float) -> None:
            return None

        provider = ArvanCloudProvider(
            api_key="MU-TEST",
            base_url="https://x/v1",
            region="ir-thr-1",
            throttle=Throttle(max_rps=1000.0, wait=wait),
        )
        server = _server(ServerLifecycleState.RUNNING, provider_key="arvancloud")
        service = self._service(provider)
        service._servers = _FakeServerRepo(server)
        view = await service.detail(server.user_id, server.id)
        assert view is not None
        assert {a.action.value for a in view.actions} <= {"power_on", "power_off", "reboot"}
        # and every advertised POWER-gated control IS present (none hidden)
        assert [a.action.value for a in view.actions] == ["power_off", "reboot"]


class _FakeCatalogRepo:
    def __init__(self, offers: list[CatalogOffer]) -> None:
        self._offers = offers
        self.list_offers = AsyncMock(return_value=offers)


class _FakeLocationRepo:
    def __init__(self, records: dict[str, list[LocationRecord]]) -> None:
        self._records = records
        self.list_for_provider = AsyncMock(side_effect=lambda key: self._records.get(key, []))


def _offer(
    provider_key: str,
    location_id: str,
    *,
    plan_id: str = "12",
    enabled: bool = True,
    vcpu: int = 2,
    name: str = "plan",
    currency: str = "EUR",
) -> CatalogOffer:
    return CatalogOffer(
        id=uuid.uuid4(),
        provider_key=provider_key,
        plan_id=plan_id,
        location_id=location_id,
        name=name,
        architecture="x86_64",
        vcpu=vcpu,
        memory_mb=4096,
        disk_gb=40,
        currency=currency,
        price_per_quantum=100,
        quantum_seconds=3600,
        enabled=enabled,
    )


class TestSharedBuyFlow:
    """The purchase flow serves offers from ANY provider through the same
    screens (M15-003 normalized offers, M15-005 shared flow)."""

    def _service(
        self,
        offers: list[CatalogOffer],
        records: dict[str, list[LocationRecord]],
    ) -> BuyFlowViewService:
        return BuyFlowViewService(
            catalog_repo=_FakeCatalogRepo(offers),  # type: ignore[arg-type]
            location_repo=_FakeLocationRepo(records),  # type: ignore[arg-type]
            signing_key=SIGNING_KEY,
        )

    async def test_two_providers_share_one_locations_flow(self) -> None:
        offers = [
            _offer("hetzner", "fsn1", currency="EUR"),
            _offer("arvancloud", "ir-thr-1", currency="IRR"),
        ]
        records = {
            "hetzner": [
                LocationRecord(
                    provider_key="hetzner",
                    location_id="fsn1",
                    name="Helsinki",
                    country_code="FI",
                    city="Helsinki",
                ),
            ],
            "arvancloud": [
                LocationRecord(
                    provider_key="arvancloud",
                    location_id="ir-thr-1",
                    name="Tehran 1",
                    country_code="IR",
                    city="Tehran",
                ),
            ],
        }
        service = self._service(offers, records)
        views = await service.locations_screen()
        # two countries, one shared flow
        assert {v.country_code for v in views} == {"FI", "IR"}
        option_keys = {(opt.provider_key, opt.location_id) for v in views for opt in v.options}
        assert option_keys == {("hetzner", "fsn1"), ("arvancloud", "ir-thr-1")}

    async def test_provider_without_sellable_offers_is_absent(self) -> None:
        offers = [
            _offer("hetzner", "fsn1", currency="EUR"),
            _offer("arvancloud", "ir-thr-1", enabled=False, currency="IRR"),  # disabled
        ]
        records = {
            "hetzner": [
                LocationRecord(
                    provider_key="hetzner",
                    location_id="fsn1",
                    name="Helsinki",
                    country_code="FI",
                    city="Helsinki",
                ),
            ],
        }
        service = self._service(offers, records)
        views = await service.locations_screen()
        option_keys = {(opt.provider_key, opt.location_id) for v in views for opt in v.options}
        assert option_keys == {("hetzner", "fsn1")}  # arvancloud hidden: no sellable offer

    async def test_plan_screen_shares_flow_for_arvancloud_offer(self) -> None:
        offer = _offer("arvancloud", "ir-thr-1", currency="IRR")
        records = {
            "arvancloud": [
                LocationRecord(
                    provider_key="arvancloud",
                    location_id="ir-thr-1",
                    name="Tehran 1",
                    country_code="IR",
                    city="Tehran",
                ),
            ],
        }
        service = self._service([offer], records)
        view = await service.plans_screen("arvancloud", "ir-thr-1")
        assert view.provider_key == "arvancloud"
        assert view.location_id == "ir-thr-1"
        assert view.location_name == "Tehran 1"
        assert len(view.plans) == 1
        assert view.plans[0].provider_key == "arvancloud"
        assert view.plans[0].location_id == "ir-thr-1"
        assert view.plans[0].currency == "IRR"
