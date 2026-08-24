"""Tests for the delete confirmation UI (M08-009).

Acceptance: double confirmation and idempotent callback.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.navigation.domain import CallbackError, decode_callback
from cloud_platform.modules.operations.service import DeleteConfirmationService

SIGNING_KEY = "test-signing-key"
SERVER_ID = uuid4()
USER_ID = uuid4()
OTHER_USER = uuid4()


def _server(
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
    user_id: UUID = USER_ID,
) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=user_id,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        provider_server_id="prov-123",
    )


class _Repo:
    def __init__(self, server: CloudServer | None) -> None:
        self._server = server

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self._server if self._server is not None and self._server.id == server_id else None


def _service(server: CloudServer | None) -> DeleteConfirmationService:
    return DeleteConfirmationService(_Repo(server), SIGNING_KEY)  # type: ignore[arg-type]


class TestVisibility:
    async def test_deletable_state_shows_stage_one(self) -> None:
        view = await _service(_server(ServerLifecycleState.RUNNING)).screen(USER_ID, SERVER_ID)
        assert view is not None
        assert view.stage == 1
        assert view.state is ServerLifecycleState.RUNNING
        assert view.provider_key == "hetzner"
        assert view.provider_server_id == "prov-123"
        assert view.idempotency_key
        assert "permanently deletes" in view.warning

    @pytest.mark.parametrize(
        "state",
        [
            ServerLifecycleState.STOPPED,
            ServerLifecycleState.ERROR,
            ServerLifecycleState.MANUAL_REVIEW,
        ],
    )
    async def test_all_deletable_states_show(self, state: ServerLifecycleState) -> None:
        view = await _service(_server(state)).screen(USER_ID, SERVER_ID)
        assert view is not None

    @pytest.mark.parametrize(
        "state",
        [
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.DELETING,
            ServerLifecycleState.DELETED,
        ],
    )
    async def test_non_deletable_states_hide(self, state: ServerLifecycleState) -> None:
        # the delete command would reject these states: no button
        view = await _service(_server(state)).screen(USER_ID, SERVER_ID)
        assert view is None

    async def test_foreign_server_is_none(self) -> None:
        view = await _service(_server()).screen(OTHER_USER, SERVER_ID)
        assert view is None

    async def test_missing_server_is_none(self) -> None:
        view = await _service(None).screen(USER_ID, SERVER_ID)
        assert view is None


class TestDoubleConfirmation:
    async def test_stage_two_has_final_warning(self) -> None:
        view = await _service(_server()).screen(USER_ID, SERVER_ID, stage=2, idempotency_key="ik-1")
        assert view is not None
        assert view.stage == 2
        assert "FINAL CONFIRMATION" in view.warning

    async def test_stage_must_be_one_or_two(self) -> None:
        with pytest.raises(ValueError):
            await _service(_server()).screen(USER_ID, SERVER_ID, stage=3)

    async def test_idempotency_key_stable_across_stages(self) -> None:
        # stage 1 mints the key; the bot re-renders stage 2 with it, and the
        # execute callback must carry the SAME key all the way to the command.
        stage1 = await _service(_server()).screen(USER_ID, SERVER_ID)
        assert stage1 is not None
        stage2 = await _service(_server()).screen(
            USER_ID, SERVER_ID, stage=2, idempotency_key=stage1.idempotency_key
        )
        assert stage2 is not None

        confirm1 = decode_callback(stage1.confirm_callback, SIGNING_KEY)
        confirm2 = decode_callback(stage2.confirm_callback, SIGNING_KEY)
        execute = decode_callback(stage2.execute_callback, SIGNING_KEY)
        assert confirm1.args[1] == stage1.idempotency_key
        assert confirm2.args[1] == stage1.idempotency_key
        assert execute.args[1] == stage1.idempotency_key

    async def test_resend_of_execute_callback_replays_same_key(self) -> None:
        # the bot may re-send the execute callback (network retry, double
        # tap): the key never changes, so the command sees the same key.
        stage2 = await _service(_server()).screen(
            USER_ID, SERVER_ID, stage=2, idempotency_key="ik-stable"
        )
        assert stage2 is not None
        a = decode_callback(stage2.execute_callback, SIGNING_KEY)
        b = decode_callback(stage2.execute_callback, SIGNING_KEY)
        assert (a.flow, a.screen, a.args) == ("delete", "execute", (str(SERVER_ID), "ik-stable"))
        assert a.args == b.args

    async def test_stage_one_confirm_advances_to_stage_two(self) -> None:
        stage1 = await _service(_server()).screen(USER_ID, SERVER_ID)
        assert stage1 is not None
        confirm = decode_callback(stage1.confirm_callback, SIGNING_KEY)
        assert (confirm.flow, confirm.screen) == ("delete", "confirm")
        assert confirm.args == (str(SERVER_ID), stage1.idempotency_key)


class TestCallbacks:
    async def test_back_goes_to_detail_and_cancel_to_menu(self) -> None:
        view = await _service(_server()).screen(USER_ID, SERVER_ID)
        assert view is not None

        back = decode_callback(view.back_callback, SIGNING_KEY)
        assert (back.flow, back.screen, back.args) == ("servers", "detail", (str(SERVER_ID),))

        cancel = decode_callback(view.cancel_callback, SIGNING_KEY)
        assert (cancel.flow, cancel.screen) == ("main", "menu")

    async def test_tampered_execute_callback_rejected(self) -> None:
        view = await _service(_server()).screen(USER_ID, SERVER_ID, stage=2, idempotency_key="ik")
        assert view is not None
        tampered = view.execute_callback[:-1] + ("0" if view.execute_callback[-1] != "0" else "1")
        with pytest.raises(CallbackError):
            decode_callback(tampered, SIGNING_KEY)
        with pytest.raises(CallbackError):
            decode_callback(view.execute_callback, "other-key")

    async def test_callbacks_are_stable(self) -> None:
        a = await _service(_server()).screen(USER_ID, SERVER_ID, stage=2, idempotency_key="ik")
        b = await _service(_server()).screen(USER_ID, SERVER_ID, stage=2, idempotency_key="ik")
        assert a is not None and b is not None
        assert a.execute_callback == b.execute_callback
        assert a.confirm_callback == b.confirm_callback
        assert a.back_callback == b.back_callback


class TestServiceValidation:
    def test_empty_signing_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            DeleteConfirmationService(_Repo(None), "")  # type: ignore[arg-type]
