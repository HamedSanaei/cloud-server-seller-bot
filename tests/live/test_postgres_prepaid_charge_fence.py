"""Real PostgreSQL proofs for the prepaid debit/server transaction fence.

Each test owns a random schema on the explicit loopback scratch database.
No application database fallback, migrations, or shared-table truncation is used.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import MetaData, delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DefaultClause

from cloud_platform.db.base import Catalog as CatalogRow
from cloud_platform.db.base import LedgerEntry as LedgerRow
from cloud_platform.db.base import Provider as ProviderRow
from cloud_platform.db.base import ProviderAccount as AccountRow
from cloud_platform.db.base import Server as ServerRow
from cloud_platform.db.base import User as UserRow
from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_HOURLY_IRT,
    ServerLifecycleState,
)
from cloud_platform.modules.wallet.domain import DuplicateIdempotencyError, LedgerEntryType
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="requires an explicit scratch CLOUD_PLATFORM_TEST_POSTGRES_URL on loopback",
)


@pytest.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("prepaid fence tests require an explicit loopback scratch PostgreSQL URL")
    schema = f"prepaid_charge_fence_{uuid4().hex}"
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "5000",
            }
        },
    )
    subset = MetaData()
    for model in (UserRow, ProviderRow, AccountRow, CatalogRow, WalletRow, LedgerRow, ServerRow):
        table = model.__table__.to_metadata(subset)
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            if "uuid_generate_v4" in str(default.arg):
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif str(default.arg).upper() == "CURRENT_TIMESTAMP":
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        async with engine.begin() as connection:
            await connection.run_sync(subset.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()


async def seed(database: async_sessionmaker[AsyncSession]) -> tuple[UUID, UUID]:
    user_id, provider_id, account_id, server_id = (uuid4() for _ in range(4))
    async with database.begin() as session:
        session.add(UserRow(id=user_id, username=f"u-{user_id.hex}", email=f"{user_id}@t.test"))
        session.add(ProviderRow(id=provider_id, name=f"p-{provider_id.hex}"))
        await session.flush()
        session.add(AccountRow(id=account_id, user_id=user_id, provider_id=provider_id))
        session.add(WalletRow(id=uuid4(), user_id=user_id, balance=1000, currency="IRT"))
        await session.flush()
        session.add(
            ServerRow(
                id=server_id,
                user_id=user_id,
                provider_id=provider_id,
                provider_account_id=account_id,
                state=ServerLifecycleState.RUNNING.value,
                billing_model=BILLING_MODEL_PREPAID_HOURLY_IRT,
                price_per_quantum=10,
                currency="USD",
                quantum_seconds=3600,
            )
        )
    return user_id, server_id


async def charge(
    repo: SqlAlchemyWalletRepository, user_id: UUID, server_id: UUID, **changes: Any
) -> tuple[Any, bool]:
    arguments = {
        "entry_type": LedgerEntryType.CHARGE,
        "reference_type": "server",
        "reference_id": str(server_id),
        "description": "prepaid hour",
        "expected_currency": "IRT",
        "expected_running_server_id": server_id,
    }
    arguments.update(changes)
    delta = arguments.pop("delta", -100)
    return await repo.adjust(user_id, delta, "prepaid-hour-1", **arguments)


async def money_facts(database: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with database() as session:
        balance = int((await session.execute(select(WalletRow.balance))).scalar_one())
        count = int(
            (await session.execute(select(func.count()).select_from(LedgerRow))).scalar_one()
        )
        return balance, count


async def server_state(database: async_sessionmaker[AsyncSession], server_id: UUID) -> str:
    async with database() as session:
        return str(
            (
                await session.execute(select(ServerRow.state).where(ServerRow.id == server_id))
            ).scalar_one()
        )


async def wait_until_blocked(database: async_sessionmaker[AsyncSession], pid: int) -> None:
    async def poll() -> None:
        async with database() as session:
            while True:
                blockers = (await session.execute(select(func.pg_blocking_pids(pid)))).scalar_one()
                if blockers:
                    return
                await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout=3)


async def finish_tasks(*tasks: asyncio.Task[Any]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)


async def test_eligible_concurrent_charge_posts_exactly_once(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id, server_id = await seed(database)
    repo = SqlAlchemyWalletRepository(database)
    results = await asyncio.wait_for(
        asyncio.gather(*(charge(repo, user_id, server_id) for _ in range(4))), timeout=5
    )
    assert sum(applied for _, applied in results) == 1
    assert all(wallet.balance == 900 for wallet, _ in results)
    assert await money_facts(database) == (900, 1)
    async with database() as session:
        ledger = (await session.execute(select(LedgerRow))).scalar_one()
        assert (ledger.amount, ledger.currency, ledger.entry_type) == (100, "IRT", "charge")
        assert (ledger.reference_type, ledger.reference_id) == ("server", server_id)


@pytest.mark.parametrize(
    "mismatch",
    [
        "deleted",
        "stopped",
        "missing",
        "owner",
        "model",
        "reference_type",
        "reference_id",
        "entry_type",
        "currency",
        "credit",
    ],
)
async def test_ineligible_first_post_changes_no_money(
    database: async_sessionmaker[AsyncSession], mismatch: str
) -> None:
    user_id, server_id = await seed(database)
    changes: dict[str, Any] = {}
    async with database.begin() as session:
        if mismatch in {"deleted", "stopped"}:
            await session.execute(
                update(ServerRow).where(ServerRow.id == server_id).values(state=mismatch)
            )
        elif mismatch == "missing":
            await session.execute(delete(ServerRow).where(ServerRow.id == server_id))
        elif mismatch == "owner":
            other = uuid4()
            session.add(UserRow(id=other, username=f"u-{other.hex}", email=f"{other}@t.test"))
            await session.flush()
            await session.execute(
                update(ServerRow).where(ServerRow.id == server_id).values(user_id=other)
            )
        elif mismatch == "model":
            await session.execute(
                update(ServerRow).where(ServerRow.id == server_id).values(billing_model="hourly")
            )
        elif mismatch == "currency":
            await session.execute(update(WalletRow).values(currency="USD"))
            changes["expected_currency"] = None
        elif mismatch == "reference_type":
            changes["reference_type"] = "order"
        elif mismatch == "reference_id":
            changes["reference_id"] = str(uuid4())
        elif mismatch == "entry_type":
            changes["entry_type"] = LedgerEntryType.REFUND
        elif mismatch == "credit":
            changes["delta"] = 100
    with pytest.raises(ValueError, match="prepaid"):
        await charge(SqlAlchemyWalletRepository(database), user_id, server_id, **changes)
    assert await money_facts(database) == (1000, 0)


@pytest.mark.parametrize("transition", ["stopped", "deleted", "removed"])
async def test_accepted_charge_replays_after_server_is_no_longer_eligible(
    database: async_sessionmaker[AsyncSession], transition: str
) -> None:
    user_id, server_id = await seed(database)
    repo = SqlAlchemyWalletRepository(database)
    first, applied = await charge(repo, user_id, server_id)
    assert applied is True
    async with database.begin() as session:
        if transition == "removed":
            await session.execute(delete(ServerRow).where(ServerRow.id == server_id))
        else:
            await session.execute(
                update(ServerRow).where(ServerRow.id == server_id).values(state=transition)
            )
    replay, applied = await charge(repo, user_id, server_id)
    assert applied is False
    assert replay.balance == first.balance == 900
    with pytest.raises(DuplicateIdempotencyError):
        await charge(repo, user_id, server_id, delta=-101)
    assert await money_facts(database) == (900, 1)


@pytest.mark.parametrize("transition", ["stopped", "deleted"])
async def test_charge_lock_blocks_state_transition_until_money_commits(
    database: async_sessionmaker[AsyncSession], transition: str
) -> None:
    user_id, server_id = await seed(database)
    before_commit, allow_commit = asyncio.Event(), asyncio.Event()
    transition_started = asyncio.Event()
    transition_pid: list[int] = []

    class PausedCommitSession(AsyncSession):
        async def commit(self) -> None:
            await self.flush()
            before_commit.set()
            await asyncio.wait_for(allow_commit.wait(), timeout=4)
            await super().commit()

    paused = async_sessionmaker(
        database.kw["bind"], class_=PausedCommitSession, expire_on_commit=False
    )
    charging = asyncio.create_task(charge(SqlAlchemyWalletRepository(paused), user_id, server_id))

    async def change_state() -> None:
        async with database.begin() as session:
            transition_pid.append(
                int((await session.execute(select(func.pg_backend_pid()))).scalar_one())
            )
            transition_started.set()
            await session.execute(
                update(ServerRow).where(ServerRow.id == server_id).values(state=transition)
            )

    transitioning: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(before_commit.wait(), timeout=3)
        transitioning = asyncio.create_task(change_state())
        await asyncio.wait_for(transition_started.wait(), timeout=3)
        await wait_until_blocked(database, transition_pid[0])
        assert not transitioning.done()
        assert await money_facts(database) == (1000, 0)
        assert await server_state(database, server_id) == "running"
        allow_commit.set()
        wallet, applied = await asyncio.wait_for(charging, timeout=3)
        await asyncio.wait_for(transitioning, timeout=3)
        assert applied is True and wallet.balance == 900
        assert await money_facts(database) == (900, 1)
        assert await server_state(database, server_id) == transition
    finally:
        allow_commit.set()
        await finish_tasks(charging, *([transitioning] if transitioning is not None else []))


@pytest.mark.parametrize("transition", ["stopped", "deleted"])
async def test_state_transition_wins_lock_and_prevents_waiting_charge(
    database: async_sessionmaker[AsyncSession], transition: str
) -> None:
    user_id, server_id = await seed(database)
    charge_started = asyncio.Event()
    charge_pid: list[int] = []

    @asynccontextmanager
    async def tracked_session() -> AsyncIterator[AsyncSession]:
        async with database() as session:
            charge_pid.append(
                int((await session.execute(select(func.pg_backend_pid()))).scalar_one())
            )
            charge_started.set()
            yield session

    charging: asyncio.Task[Any] | None = None
    try:
        async with database() as deleting:
            await deleting.execute(
                select(ServerRow).where(ServerRow.id == server_id).with_for_update()
            )
            await deleting.execute(
                update(ServerRow).where(ServerRow.id == server_id).values(state=transition)
            )
            charging = asyncio.create_task(
                charge(SqlAlchemyWalletRepository(tracked_session), user_id, server_id)
            )
            await asyncio.wait_for(charge_started.wait(), timeout=3)
            await wait_until_blocked(database, charge_pid[0])
            assert await money_facts(database) == (1000, 0)
            await deleting.commit()
        with pytest.raises(ValueError, match="not eligible"):
            await asyncio.wait_for(charging, timeout=3)
        assert await money_facts(database) == (1000, 0)
        assert await server_state(database, server_id) == transition
    finally:
        if charging is not None:
            await finish_tasks(charging)
