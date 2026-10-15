"""Fast persistence-boundary checks; transaction semantics are tested on PostgreSQL."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import TelegramSuperadminAssignment, User
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.modules.users.superadmin_repository import (
    SUPERADMIN_ADVISORY_LOCK_KEY,
    SqlAlchemySuperadminRepository,
    apply_configured_superadmin,
    authorize_superadmin_transaction,
    lock_superadmin_reconciliation,
)


def _user(**overrides: Any) -> User:
    return User(
        **{
            "id": uuid4(),
            "telegram_user_id": 123456789,
            "role": "user",
            "status": "frozen",
            "username": "retained-user",
            "email": "retained@example.test",
            **overrides,
        }
    )


def _session(assignment: TelegramSuperadminAssignment | None) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = assignment
    session.execute.return_value = result
    return session


async def test_advisory_lock_is_explicit_signed_bigint() -> None:
    session = _session(None)
    assert -(2**63) <= SUPERADMIN_ADVISORY_LOCK_KEY <= 2**63 - 1
    await lock_superadmin_reconciliation(session)
    args = session.execute.call_args.args
    assert str(args[0]) == "SELECT pg_advisory_xact_lock(:lock_key)"
    assert args[1] == {"lock_key": SUPERADMIN_ADVISORY_LOCK_KEY}


async def test_grant_promotes_in_place_and_appends_atomic_system_audit() -> None:
    user = _user()
    original_id = user.id
    assignment = TelegramSuperadminAssignment(telegram_user_id=123456789, enabled=True)
    session = _session(assignment)
    assert await apply_configured_superadmin(session, user)
    assert user.id == original_id
    assert cast(Any, user).role == "admin" and cast(Any, user).status == "active"
    assert user.username == "retained-user" and user.email == "retained@example.test"
    assert assignment.user_id == original_id
    assert user.updated_at is not None
    audit = session.add.call_args.args[0]
    assert audit.actor_type == "system" and audit.actor_id is None
    assert audit.action == "telegram.superadmin.granted"
    assert audit.event_metadata["previous_role"] == "user"
    assert audit.event_metadata["previous_status"] == "frozen"
    # The helper never commits independently of the caller's user transaction.
    session.commit.assert_not_called()
    session.delete.assert_not_called()


async def test_repeated_grant_does_not_write_or_duplicate_audit() -> None:
    user = _user(role="admin", status="active")
    assignment = TelegramSuperadminAssignment(
        telegram_user_id=123456789, enabled=True, user_id=user.id
    )
    session = _session(assignment)
    assert not await apply_configured_superadmin(session, user)
    session.add.assert_not_called()
    assert user.updated_at is None and assignment.updated_at is None


@pytest.mark.parametrize("missing", [True, False])
async def test_missing_or_revoked_assignment_does_not_grant(missing: bool) -> None:
    user = _user()
    assignment = (
        None if missing else TelegramSuperadminAssignment(telegram_user_id=123456789, enabled=False)
    )
    session = _session(assignment)
    assert not await apply_configured_superadmin(session, user)
    assert cast(Any, user).role == "user" and cast(Any, user).status == "frozen"
    session.add.assert_not_called()


async def test_no_telegram_identity_never_queries_policy() -> None:
    session = _session(None)
    assert not await apply_configured_superadmin(session, _user(telegram_user_id=None))
    session.execute.assert_not_called()


@pytest.mark.parametrize("identity", [True, "123456789", 0, -1, 2**63, 1.0])
async def test_forged_non_integer_or_out_of_range_identity_fails_closed(identity: Any) -> None:
    session = _session(None)
    with pytest.raises(ValueError):
        await apply_configured_superadmin(session, _user(telegram_user_id=identity))
    session.execute.assert_not_called()
    session.add.assert_not_called()


async def test_unpersisted_identity_cannot_be_bound() -> None:
    session = _session(None)
    with pytest.raises(ValueError, match="persisted"):
        await apply_configured_superadmin(session, _user(id=None))
    session.execute.assert_not_called()


async def test_assignment_bound_to_other_user_fails_closed() -> None:
    session = _session(
        TelegramSuperadminAssignment(telegram_user_id=123456789, enabled=True, user_id=uuid4())
    )
    with pytest.raises(ValueError, match="different user"):
        await apply_configured_superadmin(session, _user())
    session.add.assert_not_called()


@pytest.mark.parametrize("invalid", [[True], ["123"], [0], [-1], [2**63], "123", None])
async def test_reconciliation_rejects_invalid_configuration_before_session_creation(
    invalid: Any,
) -> None:
    factory = MagicMock()
    repo = SqlAlchemySuperadminRepository(factory)
    with pytest.raises(ValueError):
        await repo.reconcile(invalid)
    factory.assert_not_called()


async def test_pending_binding_is_audited_even_when_role_already_active_admin() -> None:
    user = _user(role="admin", status="active")
    assignment = TelegramSuperadminAssignment(telegram_user_id=123456789, enabled=True)
    session = _session(assignment)
    assert await apply_configured_superadmin(session, user)
    assert assignment.user_id == user.id
    assert session.add.call_args.args[0].event_metadata["binding_changed"]
    assert user.updated_at is None


async def test_malformed_non_orm_object_is_not_accepted_as_identity() -> None:
    # An unrelated object without a Telegram identity is not an implicit grant.
    session = _session(None)
    with pytest.raises(AttributeError):
        await apply_configured_superadmin(session, cast(User, SimpleNamespace()))
    session.add.assert_not_called()


async def test_onboarding_does_not_reactivate_an_already_bound_user() -> None:
    user = _user(role="user", status="banned")
    assignment = TelegramSuperadminAssignment(
        telegram_user_id=123456789, enabled=True, user_id=user.id
    )
    session = _session(assignment)
    assert not await apply_configured_superadmin(session, user, onboarding=True)
    assert cast(Any, user).role == "user" and cast(Any, user).status == "banned"
    session.add.assert_not_called()


async def test_transaction_authorization_uses_fresh_persisted_active_role_and_config() -> None:
    user = _user(role="admin", status="active")
    session = _session(cast(Any, user))
    actor_result, assignment_result = MagicMock(), MagicMock()
    actor_result.scalar_one_or_none.return_value = user
    assignment_result.scalar_one_or_none.return_value = TelegramSuperadminAssignment(
        telegram_user_id=123456789, enabled=True, user_id=user.id
    )
    session.execute.side_effect = [MagicMock(), actor_result, assignment_result]
    await authorize_superadmin_transaction(session, cast(Any, user).id, [123456789])
    assert session.execute.await_count == 3
    statement = session.execute.call_args.args[0]
    assert "FOR UPDATE" in str(statement)
    assert statement.get_execution_options()["populate_existing"] is True
    session.commit.assert_not_called()


@pytest.mark.parametrize(
    ("role", "status", "configured"),
    [("user", "active", [123456789]), ("admin", "banned", [123456789]), ("admin", "active", [])],
)
async def test_transaction_authorization_rejects_role_status_or_config_mismatch(
    role: str, status: str, configured: list[int]
) -> None:
    user = _user(role=role, status=status)
    session = _session(cast(Any, user))
    with pytest.raises(PermissionDeniedError):
        await authorize_superadmin_transaction(session, cast(Any, user).id, configured)


async def test_transaction_authorization_rejects_missing_user() -> None:
    session = _session(None)
    with pytest.raises(PermissionDeniedError):
        await authorize_superadmin_transaction(session, uuid4(), [123456789])


@pytest.mark.parametrize("assignment_state", ["absent", "revoked", "unbound", "other_user"])
async def test_transaction_authorization_requires_enabled_assignment_bound_to_same_actor(
    assignment_state: str,
) -> None:
    user = _user(role="admin", status="active")
    assignment = (
        None
        if assignment_state == "absent"
        else TelegramSuperadminAssignment(
            telegram_user_id=123456789,
            enabled=assignment_state != "revoked",
            user_id=(
                None
                if assignment_state == "unbound"
                else uuid4()
                if assignment_state == "other_user"
                else user.id
            ),
        )
    )
    actor_result, assignment_result = MagicMock(), MagicMock()
    actor_result.scalar_one_or_none.return_value = user
    assignment_result.scalar_one_or_none.return_value = assignment
    session = _session(None)
    session.execute.side_effect = [MagicMock(), actor_result, assignment_result]
    with pytest.raises(PermissionDeniedError, match="enabled bound"):
        await authorize_superadmin_transaction(session, cast(Any, user).id, [123456789])
    session.add.assert_not_called()
