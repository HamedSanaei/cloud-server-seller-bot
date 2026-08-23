"""Tests for SqlAlchemyOperationRepository (mocked session)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.operations.domain import (
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository

SERVER_ID = uuid4()
KEY = "server-create:abc"


def _row(**overrides: object) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.operation_key = KEY
    row.operation_type = "server_create"
    row.resource_type = "server"
    row.resource_id = SERVER_ID
    row.provider_key = "hetzner"
    row.status = "pending"
    row.provider_response = None
    row.error = None
    row.attempts = 0
    row.created_at = None
    row.updated_at = None
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    return mock


def _repo(db: AsyncMock) -> SqlAlchemyOperationRepository:
    return SqlAlchemyOperationRepository(lambda: db)  # type: ignore[arg-type]


def _kwargs() -> dict[str, object]:
    return {
        "operation_key": KEY,
        "operation_type": OperationType.SERVER_CREATE,
        "resource_type": "server",
        "resource_id": SERVER_ID,
        "provider_key": "hetzner",
    }


class TestGetOrCreate:
    async def test_creates_pending_row(self, db: AsyncMock) -> None:
        # first execute: select (none), then refresh path
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )
        db.refresh = AsyncMock()

        op = await _repo(db).get_or_create(**_kwargs())  # type: ignore[arg-type]

        assert op.status is OperationStatus.PENDING
        assert op.operation_key == KEY
        db.add.assert_called_once()
        row = db.add.call_args[0][0]
        assert row.operation_key == KEY
        assert row.operation_type == "server_create"
        assert row.resource_type == "server"
        assert row.resource_id == SERVER_ID
        assert row.provider_key == "hetzner"
        assert row.status == "pending"

    async def test_returns_existing(self, db: AsyncMock) -> None:
        existing = _row(id=uuid4())
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: existing))
        )

        op = await _repo(db).get_or_create(**_kwargs())  # type: ignore[arg-type]

        assert op.id == existing.id
        db.add.assert_not_called()

    async def test_concurrent_create_resolves_to_winner(self, db: AsyncMock) -> None:
        winner = _row()
        none_result = MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        winner_result = MagicMock(scalars=lambda: MagicMock(first=lambda: winner))
        db.execute = AsyncMock(side_effect=[none_result, winner_result])
        db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("uq")))
        db.add = MagicMock()

        op = await _repo(db).get_or_create(**_kwargs())  # type: ignore[arg-type]

        assert op.id == winner.id
        db.rollback.assert_awaited_once()


class TestClaim:
    async def test_claim_wins(self, db: AsyncMock) -> None:
        updated = _row(status="in_flight", attempts=1)
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(rowcount=1),  # the conditional UPDATE
                MagicMock(scalars=lambda: MagicMock(first=lambda: updated)),  # re-select
            ]
        )

        op = await _repo(db).claim(uuid4())

        assert op is not None
        assert op.status is OperationStatus.IN_FLIGHT
        assert op.attempts == 1

    async def test_claim_lost(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=MagicMock(rowcount=0))

        assert await _repo(db).claim(uuid4()) is None
