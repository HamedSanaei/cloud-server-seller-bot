"""Tests for ContainmentService: audited, bounded freeze + contain + release."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.compute.domain import CloudServer
from cloud_platform.modules.compute.domain import ServerLifecycleState as State
from cloud_platform.modules.containment.domain import (
    ContainmentError,
    ServerAction,
    UserAction,
)
from cloud_platform.modules.containment.service import ContainmentService
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)

USER_ID = uuid4()
ADMIN_ID = uuid4()


def _user(status: UserStatus = UserStatus.ACTIVE, role: Role = Role.USER) -> User:
    return User(id=USER_ID, username="victim", email="v@example.com", status=status, role=role)


def _admin(role: Role = Role.ADMIN) -> User:
    return User(id=ADMIN_ID, username="ops", email="ops@example.com", role=role)


def _server(state: State) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
    )


def _service(users: AsyncMock, servers: AsyncMock, audit: AsyncMock) -> ContainmentService:
    return ContainmentService(users, servers, audit)  # type: ignore[arg-type]


def _audit_events(audit: AsyncMock) -> list:
    return [c.args[0] for c in audit.append.call_args_list]


class TestContainUser:
    async def test_freeze_and_contain_mixed_states(self) -> None:
        running = _server(State.RUNNING)
        stopped = _server(State.STOPPED)
        deleted = _server(State.DELETED)
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user())
        users.update_status = AsyncMock(side_effect=lambda uid, status: _user(status))
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[running, stopped, deleted])
        servers.save = AsyncMock(side_effect=lambda s: s)
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        result = await _service(users, servers, audit).contain_user(
            user_id=USER_ID, actor=_admin(), reason="abuse case 7"
        )

        assert result.user_action is UserAction.FROZEN
        assert result.user_status is UserStatus.FROZEN
        assert users.update_status.await_count == 1

        by_id = {s.server_id: s for s in result.servers}
        assert by_id[running.id].action is ServerAction.CONTAINED
        assert by_id[running.id].from_state is State.RUNNING
        assert by_id[running.id].to_state is State.MANUAL_REVIEW
        assert by_id[stopped.id].action is ServerAction.CONTAINED
        assert by_id[stopped.id].from_state is State.STOPPED
        assert by_id[deleted.id].action is ServerAction.SKIPPED
        assert result.contained_server_ids == (running.id, stopped.id)
        assert result.changed is True

        actions = [e.action for e in _audit_events(audit)]
        assert actions == ["user.freeze", "server.contain"]
        contain_event = _audit_events(audit)[1]
        assert contain_event.actor_type.value == "admin"
        assert contain_event.actor_id == ADMIN_ID
        assert contain_event.reason == "abuse case 7"
        assert {s["server_id"] for s in contain_event.metadata["servers"]} == {
            str(running.id),
            str(stopped.id),
        }

    async def test_replay_is_idempotent_and_silent(self) -> None:
        contained = _server(State.MANUAL_REVIEW)
        contained.contained_from = State.RUNNING
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user(UserStatus.FROZEN))
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[contained])
        servers.save = AsyncMock()
        audit = AsyncMock()

        result = await _service(users, servers, audit).contain_user(
            user_id=USER_ID, actor=_admin(), reason="re-run"
        )

        assert result.user_action is UserAction.ALREADY_FROZEN
        assert result.servers[0].action is ServerAction.ALREADY_CONTAINED
        assert result.changed is False
        assert audit.append.await_count == 0
        users.update_status.assert_not_awaited()
        servers.save.assert_not_awaited()

    async def test_banned_user_skipped_but_servers_contained(self) -> None:
        running = _server(State.RUNNING)
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user(UserStatus.BANNED))
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[running])
        servers.save = AsyncMock(side_effect=lambda s: s)
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        result = await _service(users, servers, audit).contain_user(
            user_id=USER_ID, actor=_admin(), reason="cleanup"
        )

        assert result.user_action is UserAction.SKIPPED_BANNED
        assert result.user_status is UserStatus.BANNED
        assert result.servers[0].action is ServerAction.CONTAINED
        assert [e.action for e in _audit_events(audit)] == ["server.contain"]
        users.update_status.assert_not_awaited()

    async def test_requested_server_skipped(self) -> None:
        requested = _server(State.REQUESTED)
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user())
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[requested])
        servers.save = AsyncMock()
        audit = AsyncMock()

        result = await _service(users, servers, audit).contain_user(
            user_id=USER_ID, actor=_admin(), reason="r"
        )
        assert result.servers[0].action is ServerAction.SKIPPED
        servers.save.assert_not_awaited()

    async def test_system_actor_allowed_and_audited_as_system(self) -> None:
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user())
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[])
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        result = await _service(users, servers, audit).contain_user(
            user_id=USER_ID, actor=None, reason="automated abuse sweep"
        )
        assert result.user_action is UserAction.FROZEN
        event = _audit_events(audit)[0]
        assert event.actor_type.value == "system"
        assert event.actor_id is None

    async def test_non_admin_denied_before_side_effects(self) -> None:
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user())
        servers = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(PermissionDeniedError):
            await _service(users, servers, audit).contain_user(
                user_id=USER_ID, actor=_admin(Role.USER), reason="r"
            )
        users.get.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_missing_user_raises(self) -> None:
        users = AsyncMock()
        users.get = AsyncMock(return_value=None)
        servers = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(LookupError, match="not found"):
            await _service(users, servers, audit).contain_user(
                user_id=USER_ID, actor=_admin(), reason="r"
            )

    async def test_empty_reason_rejected(self) -> None:
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user())
        servers = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(ContainmentError, match="reason"):
            await _service(users, servers, audit).contain_user(
                user_id=USER_ID, actor=_admin(), reason="   "
            )
        audit.append.assert_not_awaited()

    async def test_case_id_recorded_in_metadata(self) -> None:
        case_id = uuid4()
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user())
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[_server(State.RUNNING)])
        servers.save = AsyncMock(side_effect=lambda s: s)
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        await _service(users, servers, audit).contain_user(
            user_id=USER_ID, actor=_admin(), reason="r", case_id=case_id
        )
        events = _audit_events(audit)
        assert events[0].metadata["case_id"] == str(case_id)
        assert events[1].metadata["case_id"] == str(case_id)


class TestReleaseUser:
    async def test_release_unfreezes_and_restores_exact_states(self) -> None:
        running = _server(State.MANUAL_REVIEW)
        running.contained_from = State.RUNNING
        stopped = _server(State.MANUAL_REVIEW)
        stopped.contained_from = State.STOPPED
        plain = _server(State.RUNNING)  # not contained -> untouched
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user(UserStatus.FROZEN))
        users.update_status = AsyncMock(side_effect=lambda uid, status: _user(status))
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[running, stopped, plain])
        servers.save = AsyncMock(side_effect=lambda s: s)
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        result = await _service(users, servers, audit).release_user(
            user_id=USER_ID, actor=_admin(), reason="cleared after review"
        )

        assert result.user_action is UserAction.UNFROZEN
        assert result.user_status is UserStatus.ACTIVE
        by_id = {s.server_id: s for s in result.servers}
        assert by_id[running.id].action is ServerAction.RELEASED
        assert by_id[running.id].to_state is State.RUNNING
        assert by_id[stopped.id].to_state is State.STOPPED
        assert by_id[plain.id].action is ServerAction.SKIPPED
        assert running.state is State.RUNNING
        assert stopped.state is State.STOPPED
        assert plain.state is State.RUNNING

        actions = [e.action for e in _audit_events(audit)]
        assert actions == ["user.unfreeze", "server.release"]

    async def test_release_banned_user_refused(self) -> None:
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user(UserStatus.BANNED))
        servers = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(ContainmentError, match="banned"):
            await _service(users, servers, audit).release_user(
                user_id=USER_ID, actor=_admin(), reason="r"
            )
        users.update_status.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_release_idempotent_when_active(self) -> None:
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user())
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[_server(State.RUNNING)])
        servers.save = AsyncMock()
        audit = AsyncMock()

        result = await _service(users, servers, audit).release_user(
            user_id=USER_ID, actor=_admin(), reason="r"
        )
        assert result.user_action is UserAction.ALREADY_ACTIVE
        assert result.changed is False
        assert audit.append.await_count == 0

    async def test_release_non_admin_denied(self) -> None:
        users = AsyncMock()
        servers = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(PermissionDeniedError):
            await _service(users, servers, audit).release_user(
                user_id=USER_ID, actor=_admin(Role.USER), reason="r"
            )
        audit.append.assert_not_awaited()

    async def test_replace_release_keeps_result_immutable(self) -> None:
        """ContainmentResult is frozen; servers tuple is immutable."""
        users = AsyncMock()
        users.get = AsyncMock(return_value=_user(UserStatus.FROZEN))
        servers = AsyncMock()
        servers.list_by_user = AsyncMock(return_value=[])
        audit = AsyncMock()

        result = await _service(users, servers, audit).release_user(
            user_id=USER_ID, actor=_admin(), reason="r"
        )
        with pytest.raises(FrozenInstanceError):
            result.user_id = uuid4()  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            result.servers = ()  # type: ignore[misc]
        replaced = replace(result, servers=())
        assert replaced is not result
