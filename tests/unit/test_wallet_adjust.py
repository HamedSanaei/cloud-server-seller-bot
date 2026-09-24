"""Atomic wallet/ledger adjustment contract (USD money model).

`SqlAlchemyWalletRepository.adjust` must commit the balance mutation and
its immutable ledger fact in ONE transaction: SELECT FOR UPDATE, overdraft
refusal before mutation, idempotent replay on identical facts,
fail-closed conflict on differing facts, and rollback-together on races.
Real-PostgreSQL concurrency/transaction coverage lives in
tests/live/test_postgres_wallet_adjust.py; this file pins the logic with a
scripted session (results are served in call order, exactly as the
implementation issues them).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.wallet.domain import (
    DuplicateIdempotencyError,
    InsufficientBalanceError,
    LedgerEntryType,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


def _wallet_row(balance: int, currency: str = "USD") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), user_id=uuid4(), balance=balance, currency=currency, status="active"
    )


def _entry_row(key: str, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid4(),
        "wallet_id": None,
        "idempotency_key": key,
        "entry_type": LedgerEntryType.CHARGE,
        "amount": 300,
        "currency": "USD",
        "reference_type": "server",
        "reference_id": None,
        "description": "usage",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _ScriptedSession:
    """Session double serving queued execute/get results in call order."""

    def __init__(
        self,
        wallet_row: SimpleNamespace | None,
        entry_queue: list[Any],
        *,
        commit_error: Exception | None = None,
    ) -> None:
        self._wallet_row = wallet_row
        self._entry_queue = list(entry_queue)
        self._commit_error = commit_error
        self.added: list[Any] = []
        self.entries: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.executes = 0
        self._balance_snapshot = wallet_row.balance if wallet_row is not None else None

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def _entity(self, stmt: Any) -> str:
        try:
            described = stmt.column_descriptions
            if described:
                entity = described[0].get("entity")
                return getattr(entity, "__name__", "")
        except Exception:
            pass
        return ""

    async def execute(self, stmt: Any) -> _Result:
        self.executes += 1
        if self._entity(stmt) == "LedgerEntry":
            if self._entry_queue:
                return _Result(self._entry_queue.pop(0))
            return _Result(None)
        return _Result(self._wallet_row)

    async def get(self, model: Any, identity: Any) -> Any:
        if "Wallet" in getattr(model, "__name__", ""):
            return self._wallet_row
        return None

    def add(self, entry: Any) -> None:
        self.added.append(entry)

    async def commit(self) -> None:
        if self._commit_error is not None:
            raise self._commit_error
        self.entries.extend(self.added)
        self.added = []
        self.commits += 1

    async def refresh(self, row: Any) -> None:
        self.executes += 1
        return None

    async def rollback(self) -> None:
        self.added = []
        self.rollbacks += 1
        # Like a real rollback, undo the pre-commit balance mutation.
        if self._wallet_row is not None and self._balance_snapshot is not None:
            self._wallet_row.balance = self._balance_snapshot


def _repo(session: _ScriptedSession) -> SqlAlchemyWalletRepository:
    return SqlAlchemyWalletRepository(lambda: session)  # type: ignore[arg-type]


class TestAdjustValidation:
    async def test_zero_delta_rejected(self) -> None:
        repo = _repo(_ScriptedSession(_wallet_row(1000), []))
        with pytest.raises(ValueError):
            await repo.adjust(uuid4(), 0, "k", entry_type=LedgerEntryType.CHARGE)

    async def test_bool_delta_rejected(self) -> None:
        repo = _repo(_ScriptedSession(_wallet_row(1000), []))
        with pytest.raises(ValueError):
            await repo.adjust(uuid4(), True, "k", entry_type=LedgerEntryType.CHARGE)  # type: ignore[arg-type]

    async def test_empty_key_rejected(self) -> None:
        repo = _repo(_ScriptedSession(_wallet_row(1000), []))
        with pytest.raises(ValueError):
            await repo.adjust(uuid4(), -100, "  ", entry_type=LedgerEntryType.CHARGE)

    async def test_missing_wallet_rejected(self) -> None:
        repo = _repo(_ScriptedSession(None, []))
        with pytest.raises(ValueError, match="no wallet"):
            await repo.adjust(uuid4(), -100, "k", entry_type=LedgerEntryType.CHARGE)


class TestAdjustDebitAndCredit:
    async def test_successful_debit_moves_money_and_posts_fact(self) -> None:
        row = _wallet_row(1000)
        session = _ScriptedSession(row, [None])
        repo = _repo(session)
        wallet, applied = await repo.adjust(
            row.user_id,
            -300,
            "charge-1",
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=str(uuid4()),
            description="usage",
        )
        assert applied is True
        assert wallet.balance == 700
        assert row.balance == 700
        assert session.commits == 1
        assert len(session.entries) == 1
        posted = session.entries[0]
        assert int(posted.amount) == 300
        assert posted.entry_type == LedgerEntryType.CHARGE
        assert posted.idempotency_key == "charge-1"
        assert posted.currency == "USD"  # the wallet's actual currency

    async def test_successful_credit(self) -> None:
        row = _wallet_row(1000)
        session = _ScriptedSession(row, [None])
        repo = _repo(session)
        wallet, applied = await repo.adjust(
            row.user_id,
            500,
            "credit-1",
            entry_type=LedgerEntryType.DEPOSIT,
            reference_type="gateway",
            description="top-up",
        )
        assert applied is True
        assert wallet.balance == 1500
        assert len(session.entries) == 1


class TestAdjustReplayAndConflict:
    async def test_identical_replay_moves_no_money(self) -> None:
        row = _wallet_row(700)
        key = "charge-1"
        server_ref = str(uuid4())
        prior = _entry_row(
            key,
            wallet_id=row.id,
            entry_type=LedgerEntryType.CHARGE,
            amount=300,
            reference_type="server",
            reference_id=server_ref,
            description="usage",
        )
        session = _ScriptedSession(row, [prior])
        repo = _repo(session)
        wallet, applied = await repo.adjust(
            row.user_id,
            -300,
            key,
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=server_ref,
            description="usage",
        )
        assert applied is False
        assert wallet.balance == 700
        assert row.balance == 700
        assert session.commits == 0
        assert session.entries == []
        assert session.rollbacks == 1

    async def test_insufficient_balance_changes_nothing(self) -> None:
        row = _wallet_row(100)
        session = _ScriptedSession(row, [None])
        repo = _repo(session)
        with pytest.raises(InsufficientBalanceError):
            await repo.adjust(row.user_id, -300, "charge-2", entry_type=LedgerEntryType.CHARGE)
        assert row.balance == 100
        assert session.commits == 0
        assert session.entries == []

    async def test_conflicting_replay_facts_fail_closed(self) -> None:
        row = _wallet_row(700)
        prior = _entry_row(
            "charge-1",
            wallet_id=row.id,
            entry_type=LedgerEntryType.CHARGE,
            amount=300,
            reference_type="server",
            reference_id=str(uuid4()),
            description="usage",
        )
        session = _ScriptedSession(row, [prior])
        repo = _repo(session)
        with pytest.raises(DuplicateIdempotencyError):
            await repo.adjust(
                row.user_id,
                -999,  # different amount under the same key
                "charge-1",
                entry_type=LedgerEntryType.CHARGE,
                reference_type="server",
                reference_id="srv-1",
                description="usage",
            )
        assert row.balance == 700
        assert session.commits == 0

    async def test_unique_race_rolls_back_and_replays_quietly(self) -> None:
        row = _wallet_row(1000)
        server_ref = str(uuid4())
        winner = _entry_row(
            "charge-1",
            wallet_id=row.id,
            entry_type=LedgerEntryType.CHARGE,
            amount=300,
            reference_type="server",
            reference_id=server_ref,
            description="usage",
        )

        class _RaceError(Exception):
            pass

        race = IntegrityError("duplicate", {}, _RaceError("uq_ledger_wallet_idempotency"))
        session = _ScriptedSession(row, [None], commit_error=race)
        # The race path re-reads the winner after rollback.
        session._entry_queue.append(winner)
        repo = _repo(session)
        wallet, applied = await repo.adjust(
            row.user_id,
            -300,
            "charge-1",
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=server_ref,
            description="usage",
        )
        assert applied is False
        assert wallet.balance == 1000
        assert session.rollbacks == 1
        assert session.commits == 0

    async def test_unique_race_with_conflicting_winner_fails_closed(self) -> None:
        row = _wallet_row(1000)
        winner = _entry_row(
            "charge-1",
            wallet_id=row.id,
            entry_type=LedgerEntryType.CHARGE,
            amount=111,  # someone else's movement under our key
            reference_type="server",
            reference_id=str(uuid4()),
            description="usage",
        )

        class _RaceError(Exception):
            pass

        race = IntegrityError("duplicate", {}, _RaceError("uq_ledger_wallet_idempotency"))
        session = _ScriptedSession(row, [None], commit_error=race)
        session._entry_queue.append(winner)
        repo = _repo(session)
        with pytest.raises(DuplicateIdempotencyError):
            await repo.adjust(
                row.user_id,
                -300,
                "charge-1",
                entry_type=LedgerEntryType.CHARGE,
                reference_type="server",
                reference_id=str(uuid4()),
                description="usage",
            )
