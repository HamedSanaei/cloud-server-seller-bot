"""Hold lifecycle coverage for SqlAlchemyHoldRepository (wallet module).

NOTE on the task path: ``src/cloud_platform/modules/billing/repository.py``
holds accrual periods, not holds. The hold lifecycle (create/duplicate-key,
release idempotent + missing, capture success/insufficient/terminal-noop,
get_by_idempotency miss) lives ONLY in
``src/cloud_platform/modules/wallet/repository.py::SqlAlchemyHoldRepository``,
which is what this file covers. Results are served in call order with the
same scripted-session shape as the wallet adjust tests.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.wallet.domain import (
    HoldStatus,
    InsufficientBalanceError,
    InsufficientHoldBalanceError,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyHoldRepository

WALLET_ID = uuid4()


class _Result:
    """One queued execute result supporting every accessor the repo uses."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalar(self) -> Any:
        return self._value

    def first(self) -> Any:
        return self._value

    def all(self) -> Any:
        return self._value

    def scalars(self) -> _Result:
        return self


class _ScriptedSession:
    """Session double serving queued execute results in call order."""

    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.added: list[Any] = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshed: list[Any] = []

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> _Result:
        assert self._results, "no queued result left for execute"
        return self._results.pop(0)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def refresh(self, row: Any) -> None:
        self.refreshed.append(row)


class _Factory:
    """Session factory serving one scripted session per repository session block."""

    def __init__(self, sessions: list[_ScriptedSession]) -> None:
        self._sessions = list(sessions)
        self.calls = 0

    def __call__(self) -> _ScriptedSession:
        self.calls += 1
        assert self._sessions, "no scripted session left"
        return self._sessions.pop(0)


def _repo(factory: _Factory) -> SqlAlchemyHoldRepository:
    return SqlAlchemyHoldRepository(factory)  # type: ignore[arg-type]


def _wallet_row(balance: int) -> SimpleNamespace:
    return SimpleNamespace(id=WALLET_ID, balance=balance)


def _hold_row(
    *,
    status: str = "created",
    amount: int = 300,
    key: str = "hold-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        wallet_id=WALLET_ID,
        amount=amount,
        currency="USD",
        idempotency_key=key,
        id=uuid4(),
        status=status,
        created_at=datetime(2026, 4, 1, 10, 0, 0),
        captured_at=None,
        released_at=None,
    )


def _create_sessions(
    *,
    existing: Any = None,
    balance: int = 1000,
    held: int = 0,
    dup: Any = None,
    created: Any | None = "default",
) -> _Factory:
    """Script the three session blocks of create_hold (check / lock / re-read)."""
    if created == "default":
        created = _hold_row()
    return _Factory(
        [
            _ScriptedSession([_Result(existing)]),
            _ScriptedSession([_Result(_wallet_row(balance)), _Result(held), _Result(dup)]),
            _ScriptedSession([_Result(created)]),
        ]
    )


class TestCreateHold:
    async def test_success_persists_hold_and_returns_it(self) -> None:
        persisted = _hold_row(amount=300)
        factory = _create_sessions(created=persisted)

        hold = await _repo(factory).create_hold(WALLET_ID, 300, "USD", "hold-1")

        assert hold.amount == 300
        assert hold.currency == "USD"
        assert hold.idempotency_key == "hold-1"
        assert hold.status is HoldStatus.CREATED
        assert factory.calls == 3
        # The returned hold is the re-read row, not the transient insert.
        assert hold.id == persisted.id

    async def test_lock_session_writes_one_row(self) -> None:
        sessions = [
            _ScriptedSession([_Result(None)]),
            _ScriptedSession([_Result(_wallet_row(1000)), _Result(0), _Result(None)]),
            _ScriptedSession([_Result(_hold_row())]),
        ]
        factory = _Factory(sessions)

        await _repo(factory).create_hold(WALLET_ID, 300, "USD", "hold-1")

        lock_session = sessions[1]
        assert lock_session.commits == 1
        assert len(lock_session.added) == 1
        assert lock_session.added[0].amount == 300
        assert lock_session.added[0].currency == "USD"
        assert lock_session.added[0].idempotency_key == "hold-1"
        assert lock_session.added[0].wallet_id == WALLET_ID

    async def test_duplicate_key_replays_existing_created_hold(self) -> None:
        existing = _hold_row(amount=300)
        factory = _Factory([_ScriptedSession([_Result(existing)])])

        hold = await _repo(factory).create_hold(WALLET_ID, 300, "USD", "hold-1")

        assert hold.id == existing.id
        assert hold.status is HoldStatus.CREATED
        assert factory.calls == 1  # replayed before any wallet lock

    async def test_terminal_existing_key_creates_a_new_hold(self) -> None:
        fresh = _hold_row(amount=300)
        factory = _Factory(
            [
                _ScriptedSession([_Result(_hold_row(status="released"))]),
                _ScriptedSession([_Result(_wallet_row(1000)), _Result(0), _Result(None)]),
                _ScriptedSession([_Result(fresh)]),
            ]
        )

        hold = await _repo(factory).create_hold(WALLET_ID, 300, "USD", "hold-1")

        assert hold.id == fresh.id
        assert hold.status is HoldStatus.CREATED

    async def test_insufficient_available_balance_fails_closed(self) -> None:
        factory = _create_sessions(balance=1000, held=900, created=None)

        with pytest.raises(InsufficientHoldBalanceError, match="available balance"):
            await _repo(factory).create_hold(WALLET_ID, 200, "USD", "hold-1")

        assert factory.calls == 2  # check + wallet lock; no insert, no re-read

    async def test_missing_wallet_raises(self) -> None:
        factory = _Factory(
            [
                _ScriptedSession([_Result(None)]),
                _ScriptedSession([_Result(None)]),
            ]
        )

        with pytest.raises(ValueError, match="not found"):
            await _repo(factory).create_hold(WALLET_ID, 100, "USD", "hold-1")

    async def test_duplicate_seen_under_lock_returns_without_insert(self) -> None:
        dup = _hold_row(amount=300)
        sessions = [
            _ScriptedSession([_Result(None)]),
            _ScriptedSession([_Result(_wallet_row(1000)), _Result(0), _Result(dup)]),
            _ScriptedSession([_Result(dup)]),
        ]
        factory = _Factory(sessions)

        hold = await _repo(factory).create_hold(WALLET_ID, 300, "USD", "hold-1")

        assert hold.id == dup.id
        assert sessions[1].added == []
        assert sessions[1].commits == 0


class TestReleaseHold:
    async def test_success_marks_released(self) -> None:
        row = _hold_row(status="created")
        session = _ScriptedSession([_Result(row)])

        hold = await _repo(_Factory([session])).release_hold(row.id)

        assert hold is not None
        assert hold.status is HoldStatus.RELEASED
        assert hold.released_at is not None
        assert row.status == "released"
        assert session.commits == 1
        assert session.refreshed == [row]

    async def test_missing_hold_returns_none(self) -> None:
        session = _ScriptedSession([_Result(None)])

        assert await _repo(_Factory([session])).release_hold(uuid4()) is None
        assert session.commits == 0

    async def test_terminal_hold_is_idempotent_noop(self) -> None:
        session = _ScriptedSession([_Result(_hold_row(status="captured"))])

        assert await _repo(_Factory([session])).release_hold(uuid4()) is None
        assert session.commits == 0


class TestCaptureHold:
    async def test_success_debits_wallet_and_marks_captured(self) -> None:
        row = _hold_row(status="created", amount=300)
        wallet = _wallet_row(1000)
        session = _ScriptedSession([_Result(row), _Result(wallet)])

        hold = await _repo(_Factory([session])).capture_hold(row.id)

        assert hold is not None
        assert hold.status is HoldStatus.CAPTURED
        assert hold.captured_at is not None
        assert wallet.balance == 700
        assert row.status == "captured"
        assert session.commits == 1
        assert session.refreshed == [row]

    async def test_insufficient_balance_rolls_back(self) -> None:
        row = _hold_row(status="created", amount=300)
        session = _ScriptedSession([_Result(row), _Result(_wallet_row(100))])

        with pytest.raises(InsufficientBalanceError, match="capture amount"):
            await _repo(_Factory([session])).capture_hold(row.id)

        assert session.rollbacks == 1
        assert session.commits == 0
        assert row.status == "created"

    async def test_terminal_hold_is_idempotent_noop(self) -> None:
        session = _ScriptedSession([_Result(_hold_row(status="released"))])

        assert await _repo(_Factory([session])).capture_hold(uuid4()) is None
        assert session.commits == 0

    async def test_missing_hold_returns_none(self) -> None:
        session = _ScriptedSession([_Result(None)])

        assert await _repo(_Factory([session])).capture_hold(uuid4()) is None


class TestGetByIdempotency:
    async def test_miss_returns_none(self) -> None:
        session = _ScriptedSession([_Result(None)])

        assert await _repo(_Factory([session])).get_by_idempotency(WALLET_ID, "nope") is None

    async def test_hit_returns_hold(self) -> None:
        row = _hold_row(key="hold-9")
        session = _ScriptedSession([_Result(row)])

        hold = await _repo(_Factory([session])).get_by_idempotency(WALLET_ID, "hold-9")

        assert hold is not None
        assert hold.id == row.id
        assert hold.status is HoldStatus.CREATED
