"""Tests for the SQLAlchemy user repository (mocked session, no live DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.modules.users.domain import User, UserNotFound, UserStatus
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository


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
