"""Persisted USD-to-IRT wallet cutover: immutable facts, guards and replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from cloud_platform.db.base import LedgerEntry, WalletCurrencyMigration
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose
from cloud_platform.modules.wallet.domain import (
    WalletCurrencyMigrationError,
    WalletCurrencyMigrationPlan,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository


class _Result:
    def __init__(self, value: Any = None, *, rows: list[Any] | None = None) -> None:
        self.value = value
        self.rows = rows or []

    def scalar_one_or_none(self) -> Any:
        return self.value

    def scalar_one(self) -> Any:
        return self.value

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self.rows


class _Session:
    def __init__(self, *, balance: int = 100, currency: str = "USD") -> None:
        self.wallet = SimpleNamespace(
            id=uuid4(),
            user_id=uuid4(),
            balance=balance,
            currency=currency,
            status="active",
            created_at=None,
            updated_at=None,
        )
        self.audit: WalletCurrencyMigration | None = None
        self.entries: dict[Any, LedgerEntry] = {}
        self.pending: list[Any] = []
        self.blockers: dict[str, bool] = {}
        self.queries: dict[str, str] = {}
        self.commit_error: Exception | None = None
        self.commits = 0
        self.rollbacks = 0
        self.balance_before = balance
        self.currency_before = currency
        self.now = datetime.now(UTC)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        sql = str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        entity = stmt.column_descriptions[0].get("entity")
        if entity is not None:
            key = entity.__name__
        elif "clock_timestamp" in sql:
            key = "clock"
        else:
            raise AssertionError(sql)
        self.queries[key] = sql
        if key == "Wallet":
            assert "FOR UPDATE" in sql
            return _Result(self.wallet)
        if key == "WalletCurrencyMigration":
            if "ORDER BY" in sql:
                return _Result(rows=[self.audit] if self.audit else [])
            return _Result(self.audit)
        if key == "clock":
            return _Result(self.now)
        if key in ("LedgerEntry", "Hold", "PaymentSession", "Server"):
            return _Result(uuid4() if self.blockers.get(key) else None)
        raise AssertionError(key)

    def add(self, row: Any) -> None:
        self.pending.append(row)

    async def get(self, model: Any, identity: Any) -> Any:
        if model is LedgerEntry:
            return self.entries.get(identity)
        raise AssertionError(model)

    async def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        for row in self.pending:
            if isinstance(row, WalletCurrencyMigration):
                self.audit = row
            elif isinstance(row, LedgerEntry):
                self.entries[row.id] = row
            else:
                raise AssertionError(row)
        self.pending.clear()
        self.commits += 1
        self.balance_before = self.wallet.balance
        self.currency_before = self.wallet.currency

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.pending.clear()
        self.wallet.balance = self.balance_before
        self.wallet.currency = self.currency_before

    async def refresh(self, row: Any) -> None:
        return None


def _plan(session: _Session, *, source: int | None = None) -> WalletCurrencyMigrationPlan:
    amount = session.wallet.balance if source is None else source
    snapshot = ConversionSnapshot(
        source_amount_minor=amount,
        source_currency="USD",
        target_amount_minor=amount * 1000,
        target_currency="IRT",
        rate=Decimal("100000"),
        purpose=FxPurpose.LIQUIDATION,
        source="abantether",
        path="USDTIRT.sell (proxy USDT for USD)",
        observed_at=session.now - timedelta(seconds=5),
        proxy=True,
        proxy_asset="USDT",
        expires_at=session.now + timedelta(minutes=5),
    )
    return WalletCurrencyMigrationPlan(
        id=uuid4(),
        user_id=session.wallet.user_id,
        wallet_id=session.wallet.id,
        source_balance_minor=amount,
        snapshot=snapshot,
        operator_id=uuid4(),
        reason="Operator approved denomination migration",
    )


def _repository(session: _Session) -> SqlAlchemyWalletRepository:
    return SqlAlchemyWalletRepository(lambda: session)  # type: ignore[arg-type]


async def test_cutover_persists_new_balance_and_separated_ledger_epochs() -> None:
    session = _Session()
    plan = _plan(session)
    wallet, applied = await _repository(session).apply_currency_migration(plan)

    assert applied is True
    assert (wallet.currency, wallet.balance) == ("IRT", 100_000)
    assert session.commits == 1
    assert session.audit is not None
    assert session.audit.snapshot == plan.snapshot.to_dict()
    assert session.audit.source_balance_minor == 100
    assert session.audit.operator_id == plan.operator_id
    assert session.audit.reason == plan.reason
    assert len(session.entries) == 2
    close = session.entries[session.audit.close_entry_id]
    opened = session.entries[session.audit.open_entry_id]
    assert (close.amount, close.currency, close.entry_type, close.idempotency_key) == (
        100,
        "USD",
        "currency_close",
        f"currency-close-{plan.id}",
    )
    assert (opened.amount, opened.currency, opened.entry_type, opened.idempotency_key) == (
        100_000,
        "IRT",
        "currency_open",
        f"currency-open-{plan.id}",
    )
    assert close.created_at < session.audit.created_at.replace(tzinfo=None) < opened.created_at
    assert close.reference_id == opened.reference_id == plan.id
    assert "holds.status = 'created'" in session.queries["Hold"]
    assert "holds.captured_at IS NULL" in session.queries["Hold"]
    assert "ledger.created_at >= " in session.queries["LedgerEntry"]
    assert "ledger.currency != 'USD'" in session.queries["LedgerEntry"]
    payments_sql = session.queries["PaymentSession"]
    assert all(status in payments_sql for status in ("'pending'", "'manual_review'", "'succeeded'"))
    assert "payment_sessions.credited_at IS NULL" in payments_sql
    assert "servers.state != 'deleted'" in session.queries["Server"]

    records = await _repository(session).list_currency_migrations(wallet.id)
    assert len(records) == 1
    assert records[0].snapshot == plan.snapshot
    assert records[0].created_at == session.audit.created_at
    assert records[0].close_entry_id == close.id
    assert records[0].open_entry_id == opened.id


async def test_exact_replay_after_quote_expiry_does_not_move_money() -> None:
    session = _Session()
    plan = _plan(session)
    await _repository(session).apply_currency_migration(plan)
    session.now = plan.snapshot.expires_at + timedelta(days=1)
    wallet, applied = await _repository(session).apply_currency_migration(plan)
    assert applied is False
    assert wallet.balance == 100_000
    assert session.commits == 1
    assert len(session.entries) == 2


async def test_changed_replay_facts_and_corrupt_ledger_are_rejected() -> None:
    session = _Session()
    plan = _plan(session)
    await _repository(session).apply_currency_migration(plan)
    changed = WalletCurrencyMigrationPlan(
        id=plan.id,
        user_id=plan.user_id,
        wallet_id=plan.wallet_id,
        source_balance_minor=plan.source_balance_minor,
        snapshot=plan.snapshot,
        operator_id=plan.operator_id,
        reason="Different operator reason",
    )
    with pytest.raises(WalletCurrencyMigrationError, match="immutable facts"):
        await _repository(session).apply_currency_migration(changed)
    assert session.audit is not None
    session.entries[session.audit.open_entry_id].amount = 99
    with pytest.raises(WalletCurrencyMigrationError, match="ledger facts"):
        await _repository(session).apply_currency_migration(plan)
    assert session.commits == 1


@pytest.mark.parametrize("blocker", ["Hold", "PaymentSession", "Server", "LedgerEntry"])
async def test_cutover_refuses_outstanding_obligation_without_mutation(blocker: str) -> None:
    session = _Session()
    session.blockers[blocker] = True
    with pytest.raises(WalletCurrencyMigrationError):
        await _repository(session).apply_currency_migration(_plan(session))
    assert (session.wallet.currency, session.wallet.balance) == ("USD", 100)
    assert session.audit is None
    assert not session.pending
    assert session.commits == 0


async def test_changed_balance_and_expired_quote_cannot_cut_over() -> None:
    session = _Session()
    plan = _plan(session)
    session.wallet.balance = 101
    with pytest.raises(WalletCurrencyMigrationError, match="balance changed"):
        await _repository(session).apply_currency_migration(plan)
    session.wallet.balance = 100
    session.now -= timedelta(minutes=15)
    expired_plan = _plan(session)
    with pytest.raises(WalletCurrencyMigrationError, match="expired"):
        await _repository(session).apply_currency_migration(expired_plan)
    assert session.audit is None


async def test_closed_wallet_cannot_migrate_but_frozen_wallet_remains_frozen() -> None:
    session = _Session()
    session.wallet.status = "closed"
    plan = _plan(session)
    with pytest.raises(WalletCurrencyMigrationError, match="closed"):
        await _repository(session).apply_currency_migration(plan)
    assert session.audit is None
    session.wallet.status = "frozen"
    wallet, applied = await _repository(session).apply_currency_migration(plan)
    assert applied and wallet.status.value == "frozen"


async def test_zero_balance_cutover_has_audit_but_no_zero_ledger_entries() -> None:
    session = _Session(balance=0)
    plan = _plan(session)
    wallet, applied = await _repository(session).apply_currency_migration(plan)
    assert applied and (wallet.currency, wallet.balance) == ("IRT", 0)
    assert session.audit is not None
    assert (session.audit.close_entry_id, session.audit.open_entry_id) == (None, None)
    assert not session.entries
    assert (await _repository(session).apply_currency_migration(plan))[1] is False
    assert (await _repository(session).list_currency_migrations(wallet.id))[
        0
    ].snapshot == plan.snapshot


async def test_constraint_failure_rolls_back_wallet_and_all_new_facts() -> None:
    session = _Session()
    session.commit_error = IntegrityError("INSERT", {}, Exception("unique constraint"))
    with pytest.raises(WalletCurrencyMigrationError, match="conflicts"):
        await _repository(session).apply_currency_migration(_plan(session))
    assert session.rollbacks == 1
    assert (session.wallet.currency, session.wallet.balance) == ("USD", 100)
    assert session.audit is None and not session.entries and not session.pending
