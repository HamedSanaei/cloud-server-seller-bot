"""Tests for SqlAlchemyProviderAccountRepository (mocked session)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.provider_accounts.domain import (
    ProviderAccountStatus,
)
from cloud_platform.modules.provider_accounts.repository import (
    SqlAlchemyProviderAccountRepository,
)

USER_ID = uuid4()


def _row() -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.user_id = USER_ID
    row.status = "active"
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


def _repo(db: AsyncMock) -> SqlAlchemyProviderAccountRepository:
    return SqlAlchemyProviderAccountRepository(lambda: db)  # type: ignore[arg-type]


class TestGetActive:
    async def test_returns_domain_with_provider_name(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=MagicMock(first=lambda: (_row(), "hetzner")))

        account = await _repo(db).get_active(USER_ID, "hetzner")

        assert account is not None
        assert account.user_id == USER_ID
        assert account.provider_key == "hetzner"
        assert account.status is ProviderAccountStatus.ACTIVE

    async def test_none_when_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=MagicMock(first=lambda: None))
        assert await _repo(db).get_active(USER_ID, "hetzner") is None

    async def test_filters_status_active_in_sql(self, db: AsyncMock) -> None:
        from sqlalchemy.dialects import postgresql

        db.execute = AsyncMock(return_value=MagicMock(first=lambda: None))
        await _repo(db).get_active(USER_ID, "hetzner")

        stmt = db.execute.call_args[0][0]
        sql = str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "'active'" in sql
        assert "provider_accounts.status" in sql
