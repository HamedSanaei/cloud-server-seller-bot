"""Opt-in live tests for the atomic wallet/ledger adjustment contract.

Skipped unless::

    CLOUD_PLATFORM_TEST_POSTGRES_URL=postgresql+asyncpg://... (a SCRATCH database)

Unit logic (validation, replay, conflict, race rollback) is pinned in
tests/unit/test_wallet_adjust.py with a scripted session. These tests prove
what only real PostgreSQL can: row-level locking serializes concurrent
same-key adjustments to exactly-once application, and a failed insert never
leaves a balance mutation without its ledger row (or vice versa).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cloud_platform.modules.wallet.domain import (
    DuplicateIdempotencyError,
    InsufficientBalanceError,
    LedgerEntryType,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()

requires_postgres = pytest.mark.skipif(
    not DB_URL,
    reason="live PostgreSQL tests require CLOUD_PLATFORM_TEST_POSTGRES_URL",
)

TABLES = ("users", "wallets", "ledger")


@pytest.fixture(scope="module")
def _migrated_db() -> Any:
    """Prepare the scratch database once per module (alembic, else subset)."""
    import asyncio as _asyncio
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["DATABASE_URL"] = DB_URL
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        if "uuid-ossp" not in (completed.stderr or ""):
            raise AssertionError(completed.stderr[-2000:])
        _asyncio.run(_create_subset_ddl(DB_URL))
    yield None


async def _create_subset_ddl(url: str) -> None:
    """Create wallet tables on servers without ``uuid-ossp`` (see the catalog
    live test for why literal server defaults are rewritten)."""
    from sqlalchemy import MetaData, text
    from sqlalchemy.schema import DefaultClause

    from cloud_platform.db.base import Base

    subset = MetaData()
    for name in TABLES:
        table = Base.metadata.tables[name].to_metadata(subset)
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            literal = str(default.arg)
            if "uuid_generate_v4" in literal:
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif literal.strip().upper() == "CURRENT_TIMESTAMP":
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(subset.create_all)
    finally:
        await engine.dispose()


def _session_factory_for(url: str) -> Any:
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def factory_cm() -> Any:
        return factory()

    factory_cm.engine = engine  # type: ignore[attr-defined]
    return factory_cm


@pytest.fixture()
async def _clean_db(_migrated_db: Any) -> AsyncIterator[Any]:
    """Fresh engine per test (own event loop) over the migrated schema."""
    from sqlalchemy import text

    _ = _migrated_db
    session_factory = _session_factory_for(DB_URL)
    engine = session_factory.engine
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} CASCADE"))
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _make_wallet(session_factory: Any, balance: int, currency: str = "USD") -> UUID:
    from cloud_platform.db.base import User as _UserModel
    from cloud_platform.db.base import Wallet as _WalletModel

    user_id = uuid4()
    async with session_factory() as session:
        session.add(
            _UserModel(id=user_id, username=f"u-{user_id.hex[:8]}", email=f"{user_id.hex}@t.test")
        )
        session.add(_WalletModel(id=uuid4(), user_id=user_id, balance=balance, currency=currency))
        await session.commit()
    return user_id


async def _ledger_count(session_factory: Any, user_id: UUID) -> int:
    from cloud_platform.db.base import LedgerEntry as _LEModel
    from cloud_platform.db.base import Wallet as _WalletModel

    async with session_factory() as session:
        wallet_id = (
            await session.execute(select(_WalletModel.id).where(_WalletModel.user_id == user_id))
        ).scalar_one()
        count = (
            await session.execute(select(func.count()).where(_LEModel.wallet_id == wallet_id))
        ).scalar_one()
        return int(count)


async def _balance_of(session_factory: Any, user_id: UUID) -> int:
    from cloud_platform.db.base import Wallet as _WalletModel

    async with session_factory() as session:
        row = (
            await session.execute(select(_WalletModel).where(_WalletModel.user_id == user_id))
        ).scalar_one()
        return int(row.balance)


@requires_postgres
class TestLiveWalletAdjust:
    async def test_debit_posts_matching_ledger_row(self, _clean_db: Any) -> None:
        user_id = await _make_wallet(_clean_db, 1000)
        repo = SqlAlchemyWalletRepository(_clean_db)
        wallet, applied = await repo.adjust(
            user_id,
            -300,
            "live-charge-1",
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=str(uuid4()),
            description="usage",
        )
        assert applied is True
        assert wallet.balance == 700
        assert await _ledger_count(_clean_db, user_id) == 1

    async def test_credit(self, _clean_db: Any) -> None:
        user_id = await _make_wallet(_clean_db, 1000)
        repo = SqlAlchemyWalletRepository(_clean_db)
        wallet, applied = await repo.adjust(
            user_id,
            500,
            "live-credit-1",
            entry_type=LedgerEntryType.DEPOSIT,
            reference_type="gateway",
            description="top-up",
        )
        assert applied is True
        assert wallet.balance == 1500

    async def test_identical_replay_moves_no_money(self, _clean_db: Any) -> None:
        user_id = await _make_wallet(_clean_db, 1000)
        repo = SqlAlchemyWalletRepository(_clean_db)
        first, second = None, None
        server_ref = str(uuid4())
        first, ok_first = await repo.adjust(
            user_id,
            -300,
            "live-charge-2",
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=server_ref,
            description="usage",
        )
        assert ok_first is True
        second, ok_second = await repo.adjust(
            user_id,
            -300,
            "live-charge-2",
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=server_ref,
            description="usage",
        )
        assert ok_second is False
        assert first.balance == second.balance == 700
        assert await _ledger_count(_clean_db, user_id) == 1

    async def test_insufficient_balance_changes_nothing(self, _clean_db: Any) -> None:
        user_id = await _make_wallet(_clean_db, 100)
        repo = SqlAlchemyWalletRepository(_clean_db)
        with pytest.raises(InsufficientBalanceError):
            await repo.adjust(user_id, -300, "live-charge-3", entry_type=LedgerEntryType.CHARGE)
        assert await _balance_of(_clean_db, user_id) == 100
        assert await _ledger_count(_clean_db, user_id) == 0

    async def test_conflicting_replay_fails_closed(self, _clean_db: Any) -> None:
        user_id = await _make_wallet(_clean_db, 1000)
        repo = SqlAlchemyWalletRepository(_clean_db)
        _, ok_first = await repo.adjust(
            user_id,
            -300,
            "live-charge-4",
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=str(uuid4()),
            description="usage",
        )
        assert ok_first is True
        with pytest.raises(DuplicateIdempotencyError):
            await repo.adjust(
                user_id,
                -999,
                "live-charge-4",
                entry_type=LedgerEntryType.CHARGE,
                reference_type="server",
                reference_id=str(uuid4()),
                description="usage",
            )
        assert await _balance_of(_clean_db, user_id) == 700
        assert await _ledger_count(_clean_db, user_id) == 1

    async def test_concurrent_same_key_applies_exactly_once(self, _clean_db: Any) -> None:
        user_id = await _make_wallet(_clean_db, 10000)
        repo = SqlAlchemyWalletRepository(_clean_db)
        server_ref = str(uuid4())

        async def _once() -> bool:
            _, applied = await repo.adjust(
                user_id,
                -100,
                "live-charge-race",
                entry_type=LedgerEntryType.CHARGE,
                reference_type="server",
                reference_id=server_ref,
                description="usage",
            )
            return applied

        results = await asyncio.gather(*(_once() for _ in range(8)))
        assert sum(1 for applied in results if applied) == 1
        assert await _balance_of(_clean_db, user_id) == 9900
        assert await _ledger_count(_clean_db, user_id) == 1

    async def test_concurrent_conflict_leaves_winner_only(self, _clean_db: Any) -> None:
        """Different facts under one key: exactly one movement survives; the
        losers fail closed and the balance matches the single winner."""
        user_id = await _make_wallet(_clean_db, 10000)
        repo = SqlAlchemyWalletRepository(_clean_db)

        async def _once(amount: int) -> bool | str:
            try:
                _, applied = await repo.adjust(
                    user_id,
                    -amount,
                    "live-charge-clash",
                    entry_type=LedgerEntryType.CHARGE,
                    reference_type="server",
                    reference_id=str(uuid4()),
                    description="usage",
                )
                return applied
            except DuplicateIdempotencyError:
                return "conflict"

        results = await asyncio.gather(*(_once(100 + index) for index in range(6)))
        applied_amounts = [100 + index for index, outcome in enumerate(results) if outcome is True]
        assert len(applied_amounts) == 1
        assert await _balance_of(_clean_db, user_id) == 10000 - applied_amounts[0]
        assert await _ledger_count(_clean_db, user_id) == 1
        assert all(outcome in (False, "conflict") for outcome in results if outcome is not True)
