"""Tests for the wallet repository (mocked session, no live DB)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.wallet.domain import (
    InsufficientBalanceError,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository


def _orm_wallet(
    user_id: int | None = None,
    balance: int = 100,
    currency: str = "EUR",
    status: str = "active",
) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.user_id = user_id or uuid4()
    row.balance = balance
    row.currency = currency
    row.status = status
    return row


@pytest.fixture
def session() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    return mock


@pytest.fixture
def repo(session: AsyncMock) -> SqlAlchemyWalletRepository:
    return SqlAlchemyWalletRepository(lambda: session)


class TestGet:
    async def test_returns_none_when_absent(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        session.get = AsyncMock(return_value=None)
        assert await repo.get(uuid4()) is None

    async def test_maps_row_to_aggregate(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(user_id=1, balance=42)
        session.get = AsyncMock(return_value=orm)
        wallet = await repo.get(uuid4())
        assert wallet is not None
        assert wallet.balance == 42


class TestGetOrCreate:
    async def test_returns_existing_when_found(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(user_id=1, balance=42)
        session.get = AsyncMock(return_value=orm)
        wallet = await repo.get_or_create(uuid4())
        assert wallet is not None
        assert wallet.balance == 42

    async def test_creates_when_missing(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        session.get = AsyncMock(return_value=None)
        session.add = MagicMock()
        session.refresh = AsyncMock(side_effect=lambda o: setattr(o, "id", uuid4()))
        wallet = await repo.get_or_create(uuid4())
        assert wallet is not None


class TestDebit:
    async def test_debits_and_commits(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=100)
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: orm))
        session.refresh = AsyncMock()
        session.get = AsyncMock(return_value=orm)

        updated = await repo.debit(uuid4(), 30, "test-key")
        assert updated.balance == 70

    async def test_insufficient_balance_raises(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=10)
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: orm))

        with pytest.raises(InsufficientBalanceError):
            await repo.debit(uuid4(), 20, "test-key")

    async def test_no_wallet_raises(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: None))

        with pytest.raises(ValueError):
            await repo.debit(uuid4(), 10, "test-key")


class TestAddFunds:
    async def test_credits_and_commits(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        orm = _orm_wallet(balance=100)
        session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=lambda: orm))
        session.refresh = AsyncMock()
        session.get = AsyncMock(return_value=orm)

        updated = await repo.add_funds(uuid4(), 50, "test-key")
        assert updated.balance == 150

    async def test_negative_amount_rejected(
        self, repo: SqlAlchemyWalletRepository, session: AsyncMock
    ) -> None:
        with pytest.raises(ValueError, match="positive"):
            await repo.add_funds(uuid4(), -10, "test-key")
