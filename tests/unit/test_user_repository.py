"""Tests for the SQLAlchemy user repository (mocked session, no live DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.modules.users.domain import User, UserNotFound, UserStatus
from cloud_platform.modules.users.repository import (
    SqlAlchemyUserRepository,
    UserStatusIdempotencyConflict,
)


def _row(
    status: str = "active",
    user_id: uuid.UUID | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = user_id or uuid.uuid4()
    row.username = "alice"
    row.email = "alice@example.com"
    row.status = status
    row.role = "user"
    row.terms_accepted_at = datetime(2026, 8, 22, tzinfo=UTC)
    row.created_at = datetime(2026, 8, 21, tzinfo=UTC)
    row.updated_at = datetime(2026, 8, 21, tzinfo=UTC)
    return row


@pytest.fixture
def session() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    return mock


@pytest.fixture
def repo(session: AsyncMock) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(lambda: session)


class TestCreate:
    async def test_adds_and_returns_aggregate_with_id(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        new_row = _row()
        captured: list = []
        session.refresh = AsyncMock(side_effect=lambda obj: captured.append(obj))

        def add_side_effect(row: object) -> None:
            row.id = new_row.id  # simulate DB-assigned PK

        session.add = MagicMock(side_effect=add_side_effect)

        user = User(username="alice", email="alice@example.com")
        created = await repo.create(user)

        session.add.assert_called_once()
        assert isinstance(created, User)
        assert created.username == "alice"
        assert created.status is UserStatus.ACTIVE

    async def test_commits_and_refreshes(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        session.refresh = AsyncMock()
        user = User(username="bob", email="bob@example.com")
        await repo.create(user)
        session.commit.assert_awaited_once()
        session.refresh.assert_awaited_once()


class TestGet:
    async def test_maps_row_to_aggregate(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        target = uuid.uuid4()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=lambda: _row(user_id=target))
        )
        found = await repo.get(target)
        assert found is not None
        assert found.id == target
        assert found.status is UserStatus.ACTIVE

    async def test_missing_user_returns_none(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        assert await repo.get(uuid.uuid4()) is None


class TestGetByFields:
    @pytest.mark.parametrize("method", ["get_by_username", "get_by_email"])
    async def test_lookup_builds_select(
        self, method: str, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: _row()))
        found: User | None = await getattr(repo, method)("alice@example.com")
        assert found is not None
        assert found.email == "alice@example.com"
        session.execute.assert_awaited_once()


class TestUpdateStatus:
    async def test_updates_and_returns_refreshed_aggregate(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        row = _row()
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: row))
        session.refresh = AsyncMock()
        updated = await repo.update_status(row.id, UserStatus.FROZEN)
        assert updated.status is UserStatus.FROZEN
        session.commit.assert_awaited_once()

    async def test_missing_user_raises_user_not_found(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))
        with pytest.raises(UserNotFound):
            await repo.update_status(uuid.uuid4(), UserStatus.BANNED)


class TestChangeStatusIdempotently:
    @staticmethod
    def _transaction(session: AsyncMock) -> AsyncMock:
        transaction = AsyncMock()
        session.begin = MagicMock(return_value=transaction)
        return transaction

    async def test_commits_bound_audit_receipt_with_status(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        transaction = self._transaction(session)
        target = _row(status="banned")
        actor_id = uuid.uuid4()
        session.execute.side_effect = [
            MagicMock(),
            MagicMock(scalar_one_or_none=lambda: target),
            MagicMock(scalar_one_or_none=lambda: None),
        ]
        result = await repo.change_status_idempotently(
            target.id,
            UserStatus.ACTIVE,
            actor_id=actor_id,
            idempotency_key="review-1",
            reason="reviewed",
        )
        assert result is UserStatus.ACTIVE
        assert target.status == "active"
        receipt = session.add.call_args.args[0]
        assert receipt.actor_type == "admin"
        assert receipt.actor_id == actor_id
        assert receipt.action == "admin.user.status_changed"
        assert receipt.resource_type == "user"
        assert receipt.resource_id == str(target.id)
        assert receipt.reason == "reviewed"
        assert receipt.event_metadata == {"status": "active", "idempotency_key": "review-1"}
        lock_params = session.execute.call_args_list[0].args[1]
        expected_lock = int.from_bytes(
            sha256(f"admin.user.status_changed:{actor_id}:review-1".encode()).digest()[:8],
            "big",
            signed=True,
        )
        assert lock_params == {"lock_key": expected_lock}
        session.flush.assert_awaited_once()
        transaction.__aexit__.assert_awaited_once_with(None, None, None)

    async def test_replay_returns_historical_status_without_reapplying(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        self._transaction(session)
        target = _row(status="banned")
        receipt = MagicMock(
            actor_type="admin",
            resource_type="user",
            resource_id=str(target.id),
            reason="reviewed",
            event_metadata={"status": "active", "idempotency_key": "review-1"},
        )
        session.execute.side_effect = [
            MagicMock(),
            MagicMock(scalar_one_or_none=lambda: target),
            MagicMock(scalar_one_or_none=lambda: receipt),
        ]
        result = await repo.change_status_idempotently(
            target.id,
            UserStatus.ACTIVE,
            actor_id=uuid.uuid4(),
            idempotency_key="review-1",
            reason="reviewed",
        )
        assert result is UserStatus.ACTIVE
        assert target.status == "banned"
        session.add.assert_not_called()
        session.flush.assert_not_awaited()

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("resource_id", str(uuid.uuid4())),
            ("resource_type", "server"),
            ("reason", "different reason"),
            ("actor_type", "user"),
            ("event_metadata", {"status": "frozen", "idempotency_key": "review-1"}),
        ],
    )
    async def test_conflicting_receipt_fails_closed(
        self,
        repo: SqlAlchemyUserRepository,
        session: AsyncMock,
        field: str,
        value: object,
    ) -> None:
        self._transaction(session)
        target = _row(status="banned")
        receipt = MagicMock(
            actor_type="admin",
            resource_type="user",
            resource_id=str(target.id),
            reason="reviewed",
            event_metadata={"status": "active", "idempotency_key": "review-1"},
        )
        setattr(receipt, field, value)
        session.execute.side_effect = [
            MagicMock(),
            MagicMock(scalar_one_or_none=lambda: target),
            MagicMock(scalar_one_or_none=lambda: receipt),
        ]
        with pytest.raises(UserStatusIdempotencyConflict):
            await repo.change_status_idempotently(
                target.id,
                UserStatus.ACTIVE,
                actor_id=uuid.uuid4(),
                idempotency_key="review-1",
                reason="reviewed",
            )
        assert target.status == "banned"
        session.add.assert_not_called()

    async def test_missing_target_does_not_append_receipt(
        self, repo: SqlAlchemyUserRepository, session: AsyncMock
    ) -> None:
        self._transaction(session)
        session.execute.side_effect = [
            MagicMock(),
            MagicMock(scalar_one_or_none=lambda: None),
            MagicMock(scalar_one_or_none=lambda: None),
        ]
        with pytest.raises(UserNotFound):
            await repo.change_status_idempotently(
                uuid.uuid4(),
                UserStatus.ACTIVE,
                actor_id=uuid.uuid4(),
                idempotency_key="review-1",
                reason="reviewed",
            )
        session.add.assert_not_called()

    @pytest.mark.parametrize(
        ("key", "reason"), [("", "reviewed"), ("  ", "reviewed"), ("key", ""), ("key", " ")]
    )
    async def test_requires_key_and_admin_reason(
        self,
        repo: SqlAlchemyUserRepository,
        session: AsyncMock,
        key: str,
        reason: str,
    ) -> None:
        with pytest.raises(ValueError):
            await repo.change_status_idempotently(
                uuid.uuid4(),
                UserStatus.ACTIVE,
                actor_id=uuid.uuid4(),
                idempotency_key=key,
                reason=reason,
            )
        session.execute.assert_not_awaited()
