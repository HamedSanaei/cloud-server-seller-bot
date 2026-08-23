"""Tests for SqlAlchemyServerRepository create + idempotency lookup (M07-001)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerCreateError,
    ServerCreateIntent,
    ServerLifecycleState,
)
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository

USER_ID = uuid4()
ACCOUNT_ID = uuid4()
PROVIDER_ID = uuid4()
CATALOG_ID = uuid4()
SERVER_ID = uuid4()


def _server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=ACCOUNT_ID,
        state=ServerLifecycleState.REQUESTED,
    )


def _intent(key: str = "key-1") -> ServerCreateIntent:
    return ServerCreateIntent(
        catalog_id=CATALOG_ID,
        cost_minor=100,
        currency="EUR",
        idempotency_key=key,
    )


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    return mock


def _repo(db: AsyncMock) -> SqlAlchemyServerRepository:
    return SqlAlchemyServerRepository(lambda: db)  # type: ignore[arg-type]


def _provider_result(row: MagicMock | None) -> MagicMock:
    return MagicMock(scalars=lambda: MagicMock(first=lambda: row))


class TestCreate:
    async def test_persists_full_row(self, db: AsyncMock) -> None:
        provider = MagicMock()
        provider.id = PROVIDER_ID
        db.execute = AsyncMock(return_value=_provider_result(provider))

        created = await _repo(db).create(_server(), _intent())

        assert created.id == SERVER_ID
        assert created.state is ServerLifecycleState.REQUESTED
        assert created.provider_key == "hetzner"
        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        row = db.add.call_args[0][0]
        assert row.provider_id == PROVIDER_ID
        assert row.provider_account_id == ACCOUNT_ID
        assert row.catalog_id == CATALOG_ID
        assert row.price_per_quantum == 100
        assert row.currency == "EUR"
        assert row.idempotency_key == "key-1"
        assert row.state == "requested"

    async def test_unknown_provider_raises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=_provider_result(None))

        with pytest.raises(LookupError, match=r"provider 'hetzner' not found"):
            await _repo(db).create(_server(), _intent())
        db.add.assert_not_called()

    async def test_constraint_violation_raises_server_create_error(self, db: AsyncMock) -> None:
        provider = MagicMock()
        provider.id = PROVIDER_ID
        db.execute = AsyncMock(return_value=_provider_result(provider))
        db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("uq")))

        with pytest.raises(ServerCreateError, match="idempotency key"):
            await _repo(db).create(_server(), _intent())
        db.rollback.assert_awaited_once()


class TestGetByIdempotencyKey:
    async def test_maps_row(self, db: AsyncMock) -> None:
        row = MagicMock()
        row.id = SERVER_ID
        row.user_id = USER_ID
        row.provider_account_id = ACCOUNT_ID
        row.state = "requested"
        row.provider_server_id = None
        row.contained_from = None
        db.execute = AsyncMock(return_value=MagicMock(first=lambda: (row, "hetzner")))

        server = await _repo(db).get_by_idempotency_key("key-1")

        assert server is not None
        assert server.id == SERVER_ID
        assert server.provider_key == "hetzner"

    async def test_none_when_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(return_value=MagicMock(first=lambda: None))
        assert await _repo(db).get_by_idempotency_key("key-1") is None
