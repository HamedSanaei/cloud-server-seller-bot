"""Scratch PostgreSQL proof of atomic deposit, ledger and business outbox writes."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cloud_platform.db.base import BusinessLogEvent, LedgerEntry, User, Wallet
from cloud_platform.modules.businesslog.events import recharge_succeeded_event
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="requires CLOUD_PLATFORM_TEST_POSTGRES_URL pointing to a scratch database"
)


@pytest.fixture(scope="module")
def migrated_payment_database():
    environment = dict(os.environ, DATABASE_URL=DB_URL)
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.fixture
async def payment_database(migrated_payment_database):
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owners = []
    event_keys = []
    try:
        yield SimpleNamespace(factory=factory, owners=owners, event_keys=event_keys)
    finally:
        async with factory() as db:
            if event_keys:
                await db.execute(
                    delete(BusinessLogEvent).where(BusinessLogEvent.event_key.in_(event_keys))
                )
            if owners:
                await db.execute(delete(User).where(User.id.in_(owners)))
            await db.commit()
        await engine.dispose()


async def seed_wallet(database, balance=7):
    owner, wallet_id, session_id = uuid4(), uuid4(), uuid4()
    event_key = f"recharge.succeeded:{session_id}"
    database.owners.append(owner)
    database.event_keys.append(event_key)
    async with database.factory() as db:
        db.add(User(id=owner, username=f"financial-{owner.hex}", email=f"{owner.hex}@example.test"))
        await db.flush()
        db.add(Wallet(id=wallet_id, user_id=owner, balance=balance, currency="IRT"))
        await db.commit()
    return owner, wallet_id, session_id, event_key


async def financial_facts(database, wallet_id, event_key):
    async with database.factory() as db:
        balance = (
            await db.execute(select(Wallet.balance).where(Wallet.id == wallet_id))
        ).scalar_one()
        ledger_count = (
            await db.execute(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.wallet_id == wallet_id)
            )
        ).scalar_one()
        events = list(
            (
                await db.execute(
                    select(BusinessLogEvent).where(BusinessLogEvent.event_key == event_key)
                )
            ).scalars()
        )
        return balance, ledger_count, events


async def test_concurrent_postgres_deposits_have_one_balance_ledger_and_outbox_effect(
    payment_database,
):
    owner, wallet_id, session_id, event_key = await seed_wallet(payment_database)
    repo = SqlAlchemyWalletRepository(payment_database.factory)
    constructed_balances = []

    def event_factory(wallet):
        constructed_balances.append(wallet.balance)
        return recharge_succeeded_event(
            user=None,
            payment_session_id=session_id,
            amount_minor=10,
            currency="IRT",
            gateway="zarinpal",
            gateway_reference="live-authority",
            balance_after_minor=wallet.balance,
        )

    async with asyncio.timeout(30):
        results = await asyncio.gather(
            *(
                repo.credit_deposit(
                    owner,
                    10,
                    f"deposit-{session_id}",
                    expected_currency="IRT",
                    reference="zarinpal/live-authority",
                    business_event_factory=event_factory,
                )
                for _ in range(8)
            )
        )
    assert sum(applied for _, applied in results) == 1
    assert constructed_balances == [17]
    balance, ledger_count, events = await financial_facts(payment_database, wallet_id, event_key)
    assert balance == 17 and ledger_count == len(events) == 1
    assert events[0].payload["balance_after"] == "17 تومان"
    assert events[0].status == "PENDING"


async def test_postgres_outbox_insert_failure_rolls_back_wallet_and_ledger(payment_database):
    owner, wallet_id, session_id, event_key = await seed_wallet(payment_database)
    async with payment_database.factory() as db:
        db.add(
            BusinessLogEvent(
                id=uuid4(),
                event_key=event_key,
                event_type="recharge.succeeded",
                payload={"immutable_marker": "existing"},
                status="PENDING",
                attempts=0,
            )
        )
        await db.commit()
    repo = SqlAlchemyWalletRepository(payment_database.factory)

    def event_factory(wallet):
        return recharge_succeeded_event(
            user=None,
            payment_session_id=session_id,
            amount_minor=10,
            currency="IRT",
            gateway="zarinpal",
            balance_after_minor=wallet.balance,
        )

    with pytest.raises(IntegrityError):
        await repo.credit_deposit(
            owner, 10, f"deposit-{session_id}", business_event_factory=event_factory
        )
    balance, ledger_count, events = await financial_facts(payment_database, wallet_id, event_key)
    assert balance == 7 and ledger_count == 0 and len(events) == 1
    assert events[0].payload == {"immutable_marker": "existing"}
