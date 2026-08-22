"""Tests for wallet hold repository: locking, available-balance check, atomic capture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.wallet.domain import (
    InsufficientBalanceError,
    InsufficientHoldBalanceError,
    Wallet,
)
from cloud_platform.modules.wallet.repository import HoldService, SqlAlchemyHoldRepository


def _session_factory(session: AsyncMock) -> MagicMock:
    factory = MagicMock(return_value=session)
    factory.__aenter__ = AsyncMock(return_value=session)
    factory.__aexit__ = AsyncMock(return_value=None)
    return factory


def _result(scalar_one_or_none=None, scalar=None) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar_one_or_none
    m.scalar.return_value = scalar
    return m


def _wallet_row(balance: int) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.balance = balance
    row.user_id = uuid4()
    row.currency = "EUR"
    row.status = "active"
    row.created_at = None
    row.updated_at = None
    return row


class TestCreateHoldAvailableBalance:
    async def test_insufficient_available_balance(self) -> None:
        """balance - active_holds < amount must raise, even if raw balance is high."""
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        # execute sequence: idempotency check, wallet lock, active hold sum
        session.execute = AsyncMock(
            side_effect=[
                _result(scalar_one_or_none=None),
                _result(scalar_one_or_none=_wallet_row(balance=500)),
                _result(scalar=400),  # 400 already held
            ]
        )
        session.rollback = AsyncMock()

        repo = SqlAlchemyHoldRepository(_session_factory(session))  # type: ignore[arg-type]
        with pytest.raises(InsufficientHoldBalanceError):
            await repo.create_hold(uuid4(), 200, "EUR", "h1")
        # 500 - 400 = 100 available < 200

    async def test_just_enough_available_balance(self) -> None:
        """available == amount succeeds."""
        wallet_id = uuid4()
        hold_id = uuid4()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        hold_row = MagicMock()
        hold_row.id = hold_id
        hold_row.wallet_id = wallet_id
        hold_row.amount = 200
        hold_row.currency = "EUR"
        hold_row.idempotency_key = "h1"
        hold_row.status = "created"
        hold_row.created_at = None
        hold_row.captured_at = None
        hold_row.released_at = None

        # execute sequence: idem check, wallet lock, hold sum, dup check,
        # then get_by_idempotency after commit
        session.execute = AsyncMock(
            side_effect=[
                _result(scalar_one_or_none=None),
                _result(scalar_one_or_none=_wallet_row(balance=500)),
                _result(scalar=300),  # 200 available
                _result(scalar_one_or_none=None),
                _result(scalar_one_or_none=hold_row),
            ]
        )
        session.commit = AsyncMock()
        session.add = MagicMock()

        repo = SqlAlchemyHoldRepository(_session_factory(session))  # type: ignore[arg-type]
        hold = await repo.create_hold(wallet_id, 200, "EUR", "h1")
        assert hold.id == hold_id
        assert hold.amount == 200


class TestCaptureAtomicDebit:
    async def test_capture_debits_wallet_atomically(self) -> None:
        hold_id = uuid4()
        wallet_id = uuid4()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        hold_row = MagicMock()
        hold_row.id = hold_id
        hold_row.wallet_id = wallet_id
        hold_row.amount = 300
        hold_row.currency = "EUR"
        hold_row.idempotency_key = "h1"
        hold_row.status = "created"
        hold_row.created_at = None
        hold_row.captured_at = None
        hold_row.released_at = None

        wallet_row = _wallet_row(balance=500)

        session.execute = AsyncMock(
            side_effect=[
                _result(scalar_one_or_none=hold_row),
                _result(scalar_one_or_none=wallet_row),
            ]
        )
        session.commit = AsyncMock()
        session.refresh = AsyncMock()

        repo = SqlAlchemyHoldRepository(_session_factory(session))  # type: ignore[arg-type]
        captured = await repo.capture_hold(hold_id)

        assert captured is not None
        assert wallet_row.balance == 200  # 500 - 300 debited
        assert hold_row.status == "captured"
        assert hold_row.captured_at is not None
        session.commit.assert_awaited_once()

    async def test_capture_insufficient_balance_rolls_back(self) -> None:
        hold_id = uuid4()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        hold_row = MagicMock()
        hold_row.id = hold_id
        hold_row.wallet_id = uuid4()
        hold_row.amount = 300
        hold_row.status = "created"

        session.execute = AsyncMock(
            side_effect=[
                _result(scalar_one_or_none=hold_row),
                _result(scalar_one_or_none=_wallet_row(balance=100)),
            ]
        )
        session.rollback = AsyncMock()
        session.commit = AsyncMock()

        repo = SqlAlchemyHoldRepository(_session_factory(session))  # type: ignore[arg-type]
        with pytest.raises(InsufficientBalanceError):
            await repo.capture_hold(hold_id)
        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_capture_terminal_hold_is_noop(self) -> None:
        hold_id = uuid4()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        hold_row = MagicMock()
        hold_row.id = hold_id
        hold_row.status = "released"

        session.execute = AsyncMock(side_effect=[_result(scalar_one_or_none=hold_row)])
        session.commit = AsyncMock()

        repo = SqlAlchemyHoldRepository(_session_factory(session))  # type: ignore[arg-type]
        assert await repo.capture_hold(hold_id) is None
        session.commit.assert_not_awaited()

    async def test_release_terminal_hold_is_noop(self) -> None:
        hold_id = uuid4()
        session = AsyncMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        hold_row = MagicMock()
        hold_row.id = hold_id
        hold_row.status = "captured"

        session.execute = AsyncMock(side_effect=[_result(scalar_one_or_none=hold_row)])
        session.commit = AsyncMock()

        repo = SqlAlchemyHoldRepository(_session_factory(session))  # type: ignore[arg-type]
        assert await repo.release_hold(hold_id) is None
        session.commit.assert_not_awaited()


class TestAvailableBalance:
    """available_balance = wallet.balance - sum(active holds)."""

    async def test_available_balance_computed(self) -> None:
        wallet_id = uuid4()
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(
            return_value=Wallet(
                user_id=uuid4(),
                id=wallet_id,
                balance=1000,
                currency="EUR",
            )
        )
        hold_repo = AsyncMock()
        hold_repo.active_hold_sum = AsyncMock(return_value=300)

        service = HoldService(wallet_repo, hold_repo, AsyncMock())  # type: ignore[arg-type]
        assert await service.available_balance(wallet_id) == 700

    async def test_available_balance_wallet_not_found(self) -> None:
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(return_value=None)

        service = HoldService(wallet_repo, AsyncMock(), AsyncMock())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match=r"wallet.*not found"):
            await service.available_balance(uuid4())

    async def test_available_balance_no_holds(self) -> None:
        wallet_id = uuid4()
        wallet_repo = AsyncMock()
        wallet_repo.get = AsyncMock(
            return_value=Wallet(
                user_id=uuid4(),
                id=wallet_id,
                balance=250,
                currency="EUR",
            )
        )
        hold_repo = AsyncMock()
        hold_repo.active_hold_sum = AsyncMock(return_value=0)

        service = HoldService(wallet_repo, hold_repo, AsyncMock())  # type: ignore[arg-type]
        assert await service.available_balance(wallet_id) == 250
