"""Concurrency tests for wallet spending (M05-007).

Acceptance: parallel reservation cannot create negative availability.

The tests have two parts:

1. ``TestParallelReservationInvariant`` runs the *reservation algorithm*
   (check ``available = balance - held`` then create the hold, all under a
   single row lock) as a faithful in-memory model of
   ``SqlAlchemyHoldRepository.create_hold`` under real ``asyncio``
   concurrency. A live PostgreSQL instance is not available in the unit
   environment, so the ``SELECT ... FOR UPDATE`` row lock is modelled by an
   ``asyncio.Lock`` that serialises the same critical section. The invariant
   under test — total reserved never exceeds the balance, so available
   balance never goes negative — is what the DB lock enforces in production.

2. ``TestCreateHoldProductionQuery`` executes the *real* repository method
   against a capturing mock session and asserts the production statements:
   the wallet select carries ``FOR UPDATE`` and the held-sum is consulted
   before the insert, and that an insufficient *available* balance (not just
   raw balance) is rejected.
"""

from __future__ import annotations

import asyncio
import random
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.wallet.domain import InsufficientHoldBalanceError
from cloud_platform.modules.wallet.repository import SqlAlchemyHoldRepository

# ---------------------------------------------------------------------------
# In-memory model of the create_hold critical section
# ---------------------------------------------------------------------------


class _InMemoryWallet:
    """Models one wallet row plus its active holds behind a row lock.

    ``lock`` models the ``SELECT ... FOR UPDATE`` the repository takes on the
    wallet row; the entire check-then-insert happens while holding it, exactly
    as the repository does inside one transaction.
    """

    def __init__(self, balance: int) -> None:
        self.balance = balance
        self.active_holds: list[int] = []
        self.lock = asyncio.Lock()

    def available(self) -> int:
        return self.balance - sum(self.active_holds)

    async def create_hold(self, amount: int) -> bool:
        """Mirror of the repository's check-then-create under the row lock.

        Returns True when the hold was created, False when availability was
        insufficient (the repository raises InsufficientHoldBalanceError).
        """
        async with self.lock:  # SELECT ... FOR UPDATE
            if self.available() < amount:
                return False
            self.active_holds.append(amount)  # INSERT hold
            return True


class TestParallelReservationInvariant:
    """Parallel reservations must never push available balance negative."""

    async def test_even_reservations_fill_exactly_to_balance(self) -> None:
        wallet = _InMemoryWallet(balance=1000)
        results = await asyncio.gather(*(wallet.create_hold(200) for _ in range(20)))
        successes = sum(results)
        assert successes == 5  # 5 * 200 == 1000, the rest are rejected
        assert wallet.available() == 0
        assert sum(wallet.active_holds) == wallet.balance

    async def test_random_reservations_never_negative(self) -> None:
        wallet = _InMemoryWallet(balance=1000)
        rng = random.Random(20260822)
        attempts = [rng.randint(1, 300) for _ in range(300)]

        async def _attempt(amount: int) -> bool:
            return await wallet.create_hold(amount)

        await asyncio.gather(*(_attempt(a) for a in attempts))

        assert wallet.available() >= 0  # the acceptance invariant
        assert sum(wallet.active_holds) <= wallet.balance

    async def test_overlarge_single_hold_rejected_without_overshoot(self) -> None:
        wallet = _InMemoryWallet(balance=1000)
        assert await wallet.create_hold(1001) is False
        assert wallet.active_holds == []
        assert wallet.available() == 1000

    async def test_concurrent_mixed_sizes_total_within_balance(self) -> None:
        wallet = _InMemoryWallet(balance=5000)
        rng = random.Random(7)
        attempts = [rng.choice([100, 250, 400, 900]) for _ in range(100)]

        await asyncio.gather(*(wallet.create_hold(a) for a in attempts))

        assert wallet.available() >= 0
        assert sum(wallet.active_holds) <= wallet.balance


# ---------------------------------------------------------------------------
# Production repository query assertions
# ---------------------------------------------------------------------------


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


def _hold_row(wallet_id, amount: int, key: str) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.wallet_id = wallet_id
    row.amount = amount
    row.currency = "EUR"
    row.idempotency_key = key
    row.status = "created"
    row.created_at = None
    row.captured_at = None
    row.released_at = None
    return row


def _result(row=None, scalar=None) -> MagicMock:
    m = MagicMock()
    m.scalar_one_or_none.return_value = row
    m.scalar.return_value = scalar
    return m


def _capturing_session_factory(results: list[MagicMock], captured: list) -> MagicMock:
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    async def _execute(stmt):
        captured.append(stmt)
        return results.pop(0)

    session.execute = _execute
    session.commit = AsyncMock()
    session.add = MagicMock()

    factory = MagicMock(return_value=session)
    factory.__aenter__ = AsyncMock(return_value=session)
    factory.__aexit__ = AsyncMock(return_value=None)
    return factory


class TestCreateHoldProductionQuery:
    async def test_wallet_select_uses_row_lock_and_held_sum(self) -> None:
        """create_hold must lock the wallet row and consult the held sum."""
        wallet_id = uuid4()
        captured: list = []
        # E1 idempotency check, E2 wallet FOR UPDATE, E3 held sum,
        # E4 duplicate check, E5 get_by_idempotency after commit
        results = [
            _result(row=None),
            _result(row=_wallet_row(balance=1000)),
            _result(scalar=0),
            _result(row=None),
            _result(row=_hold_row(wallet_id, 200, "k1")),
        ]
        factory = _capturing_session_factory(results, captured)
        repo = SqlAlchemyHoldRepository(factory)  # type: ignore[arg-type]

        hold = await repo.create_hold(wallet_id, 200, "EUR", "k1")
        assert hold.amount == 200

        wallet_stmt = captured[1]
        assert wallet_stmt._for_update_arg is not None  # SELECT ... FOR UPDATE
        held_stmt = captured[2]
        from sqlalchemy.sql.functions import FunctionElement

        assert isinstance(held_stmt._raw_columns[0], FunctionElement)  # the sum

    async def test_rejects_when_available_balance_insufficient(self) -> None:
        """Raw balance 1000 with 800 already held leaves 200 available; a
        300 hold must be rejected even though raw balance > 300."""
        wallet_id = uuid4()
        captured: list = []
        results = [
            _result(row=None),  # idempotency check
            _result(row=_wallet_row(balance=1000)),  # wallet FOR UPDATE
            _result(scalar=800),  # held sum
        ]
        factory = _capturing_session_factory(results, captured)
        repo = SqlAlchemyHoldRepository(factory)  # type: ignore[arg-type]

        with pytest.raises(InsufficientHoldBalanceError):
            await repo.create_hold(wallet_id, 300, "EUR", "k1")
        # stopped before the duplicate check / insert
        assert len(captured) == 3
