"""Tests for the power controls UI (M08-008).

Acceptance: capabilities/states hide invalid actions.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.navigation.domain import decode_callback
from cloud_platform.modules.operations.service import (
    PowerAction,
    PowerControlsService,
    available_power_actions,
)
from cloud_platform.providers.base import Capability
from cloud_platform.providers.registry import ProviderRegistry

SIGNING_KEY = "test-signing-key"
SERVER_ID = uuid4()
USER_ID = uuid4()
OTHER_USER = uuid4()


def _server(
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
    user_id: UUID = USER_ID,
    provider_key: str = "hetzner",
) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=user_id,
        provider_key=provider_key,
        provider_account_id=uuid4(),
        state=state,
        provider_server_id="prov-123",
    )


class FakeProvider:
    def __init__(self, key: str, capabilities: frozenset[Capability]) -> None:
        self.key = key
        self.capabilities = capabilities


def _service(
    server: CloudServer | None,
    providers: list[FakeProvider] | None = None,
) -> PowerControlsService:
    registry = ProviderRegistry()
    for p in providers or [
        FakeProvider("hetzner", frozenset({Capability.POWER, Capability.COMPUTE}))
    ]:
        registry.register(p)  # type: ignore[arg-type]

    class _Repo:
        async def get(self, server_id: UUID) -> CloudServer | None:
            return server if server is not None and server.id == server_id else None

    return PowerControlsService(_Repo(), registry, SIGNING_KEY)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The shared gate itself
# --------------------------------------------------------------------------


class TestAvailablePowerActions:
    def test_running_server_with_power(self) -> None:
        caps = frozenset({Capability.POWER, Capability.COMPUTE})
        assert available_power_actions(ServerLifecycleState.RUNNING, caps) == [
            PowerAction.POWER_OFF,
            PowerAction.REBOOT,
        ]

    def test_stopped_server_with_power(self) -> None:
        caps = frozenset({Capability.POWER})
        assert available_power_actions(ServerLifecycleState.STOPPED, caps) == [PowerAction.POWER_ON]

    def test_no_power_capability_hides_everything(self) -> None:
        caps = frozenset({Capability.COMPUTE})
        assert available_power_actions(ServerLifecycleState.RUNNING, caps) == []
        assert available_power_actions(ServerLifecycleState.STOPPED, caps) == []

    def test_non_steady_states_have_no_actions(self) -> None:
        caps = frozenset({Capability.POWER})
        for state in (
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
            ServerLifecycleState.ERROR,
            ServerLifecycleState.MANUAL_REVIEW,
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.DELETING,
            ServerLifecycleState.DELETED,
        ):
            assert available_power_actions(state, caps) == []


# --------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------


class TestViewHidesInvalidActions:
    async def test_running_server_shows_off_and_reboot(self) -> None:
        view = await _service(_server(ServerLifecycleState.RUNNING)).detail(USER_ID, SERVER_ID)
        assert view is not None
        assert [a.action for a in view.actions] == [
            PowerAction.POWER_OFF,
            PowerAction.REBOOT,
        ]
        assert "power: Power off, Reboot" in view.render()

    async def test_stopped_server_shows_power_on_only(self) -> None:
        view = await _service(_server(ServerLifecycleState.STOPPED)).detail(USER_ID, SERVER_ID)
        assert view is not None
        assert [a.action for a in view.actions] == [PowerAction.POWER_ON]

    async def test_provisioning_server_has_no_power_actions(self) -> None:
        view = await _service(_server(ServerLifecycleState.PROVISIONING)).detail(USER_ID, SERVER_ID)
        assert view is not None
        assert view.actions == ()
        assert "no actions available" in view.render()

    async def test_provider_without_power_capability_hides_all(self) -> None:
        provider = FakeProvider("hetzner", frozenset({Capability.COMPUTE}))
        view = await _service(_server(ServerLifecycleState.RUNNING), [provider]).detail(
            USER_ID, SERVER_ID
        )
        assert view is not None
        assert view.actions == ()

    async def test_unknown_provider_hides_all(self) -> None:
        # provider_key not registered: no capability information -> no actions
        view = await _service(
            _server(ServerLifecycleState.RUNNING, provider_key="ghost"),
            [FakeProvider("hetzner", frozenset({Capability.POWER}))],
        ).detail(USER_ID, SERVER_ID)
        assert view is not None
        assert view.actions == ()

    async def test_view_carries_server_context(self) -> None:
        view = await _service(_server(ServerLifecycleState.RUNNING)).detail(USER_ID, SERVER_ID)
        assert view is not None
        assert view.server_id == SERVER_ID
        assert view.provider_key == "hetzner"
        assert view.state is ServerLifecycleState.RUNNING
        assert view.provider_server_id == "prov-123"


class TestOwnership:
    async def test_foreign_server_is_none(self) -> None:
        view = await _service(_server(ServerLifecycleState.RUNNING)).detail(OTHER_USER, SERVER_ID)
        assert view is None

    async def test_missing_server_is_none(self) -> None:
        view = await _service(None).detail(USER_ID, SERVER_ID)
        assert view is None


class TestCallbacks:
    async def test_action_callbacks_roundtrip(self) -> None:
        view = await _service(_server(ServerLifecycleState.RUNNING)).detail(USER_ID, SERVER_ID)
        assert view is not None
        off, reboot = view.actions
        assert off.label == "Power off"
        assert reboot.label == "Reboot"

        decoded = decode_callback(off.callback, SIGNING_KEY)
        assert (decoded.flow, decoded.screen) == ("servers", "power_off")
        assert decoded.args == (str(SERVER_ID),)

        decoded = decode_callback(reboot.callback, SIGNING_KEY)
        assert (decoded.flow, decoded.screen) == ("servers", "reboot")
        assert decoded.args == (str(SERVER_ID),)

    async def test_back_and_cancel_callbacks(self) -> None:
        view = await _service(_server(ServerLifecycleState.RUNNING)).detail(USER_ID, SERVER_ID)
        assert view is not None

        back = decode_callback(view.back_callback, SIGNING_KEY)
        assert (back.flow, back.screen) == ("servers", "list")
        assert back.args == ()

        cancel = decode_callback(view.cancel_callback, SIGNING_KEY)
        assert (cancel.flow, cancel.screen) == ("main", "menu")

    async def test_tampered_action_callback_rejected(self) -> None:
        from cloud_platform.modules.navigation.domain import CallbackError

        view = await _service(_server(ServerLifecycleState.RUNNING)).detail(USER_ID, SERVER_ID)
        assert view is not None
        tampered = view.actions[0].callback.replace("power_off", "power_on")
        if tampered == view.actions[0].callback:
            tampered = view.actions[0].callback[:-1] + "x"
        with pytest.raises(CallbackError):
            decode_callback(tampered, SIGNING_KEY)

    async def test_callbacks_are_stable(self) -> None:
        first = await _service(_server(ServerLifecycleState.RUNNING)).detail(USER_ID, SERVER_ID)
        second = await _service(_server(ServerLifecycleState.RUNNING)).detail(USER_ID, SERVER_ID)
        assert first is not None and second is not None
        assert [a.callback for a in first.actions] == [a.callback for a in second.actions]
        assert first.back_callback == second.back_callback


class TestServiceValidation:
    def test_empty_signing_key_rejected(self) -> None:
        class _Repo:
            async def get(self, server_id: UUID) -> CloudServer | None:
                return None

        with pytest.raises(ValueError):
            PowerControlsService(_Repo(), ProviderRegistry(), "")  # type: ignore[arg-type]
