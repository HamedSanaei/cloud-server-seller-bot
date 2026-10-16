"""Opt-in real PostgreSQL cutover tests in a disposable loopback schema only."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DefaultClause

from cloud_platform.db import base as db
from cloud_platform.modules.funding.cutover import (
    CutoverEntry,
    CutoverManifest,
    SqlAlchemyOperatorFundingCutover,
)
from cloud_platform.modules.users.superadmin_repository import SqlAlchemySuperadminRepository

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not DB_URL, reason="explicit scratch loopback PostgreSQL required")
TELEGRAM_ID = 20202


def _financial_migration():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0056_operator_funding.py"
    spec = importlib.util.spec_from_file_location("cutover_financial_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("cutover tests require explicit scratch PostgreSQL on loopback")
    schema = f"operator_cutover_test_{uuid4().hex}"
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "application_name": schema,
                "statement_timeout": "10000",
                "lock_timeout": "4000",
                "timezone": "UTC",
            }
        },
    )
    metadata = MetaData()
    for table in db.Base.metadata.tables.values():
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            default = column.server_default
            if default is not None and "uuid_generate_v4" in str(default.arg):
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif default is not None and str(default.arg) == "CURRENT_TIMESTAMP":
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
            elif default is not None and "::jsonb" in str(default.arg):
                column.server_default = DefaultClause(text(str(default.arg)))

    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
            await connection.run_sync(metadata.create_all)
            module = _financial_migration()
            await connection.execute(text(module._IMMUTABLE_FUNCTION))
            for table in ("operator_funding_cutovers", "operator_expenses"):
                await connection.execute(
                    text(
                        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                        "FOR EACH ROW EXECUTE FUNCTION prevent_operator_financial_mutation()"
                    )
                )
            await connection.execute(
                text(
                    "CREATE UNIQUE INDEX test_cutover_ledger_unique "
                    "ON ledger(wallet_id,idempotency_key)"
                )
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        if created:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def seed(database, *, count=5):
    now = datetime.now(UTC)
    owner, provider_id, account_id = uuid4(), uuid4(), uuid4()
    async with database.begin() as session:
        session.add(
            db.User(
                id=owner,
                username=f"cutover-{owner.hex}",
                email=f"{owner.hex}@example.test",
                status="active",
                role="user",
                telegram_user_id=TELEGRAM_ID,
            )
        )
        session.add(db.Provider(id=provider_id, name="fixture-provider"))
    async with database.begin() as session:
        session.add(
            db.ProviderAccount(
                id=account_id, provider_id=provider_id, user_id=owner, status="active"
            )
        )
    entries = []
    async with database.begin() as session:
        for index in range(count):
            server_id = uuid4()
            state, native = ("deleted", "historical-accepted-id") if index == 0 else ("error", None)
            session.add(
                db.Server(
                    id=server_id,
                    user_id=owner,
                    provider_id=provider_id,
                    provider_account_id=account_id,
                    state=state,
                    provider_server_id=native,
                    credential_account_id="fixture-account",
                    billing_model="hourly",
                    price_per_quantum=99,
                    currency="USD",
                    last_accrued_at=now - timedelta(days=80),
                    deleted_at=(now - timedelta(days=60)).replace(tzinfo=None),
                    idempotency_key=f"historic:{server_id}",
                )
            )
            entries.append(
                CutoverEntry(
                    server_id,
                    owner,
                    state,
                    "fixture-provider",
                    native,
                    "fixture-account",
                    "hourly",
                    "Explicitly classify retained historical own service",
                    "Reviewed historical provider and local evidence; no active acceptance claimed",
                    expected_last_accrued_at=now - timedelta(days=80),
                )
            )
    await SqlAlchemySuperadminRepository(database).reconcile([TELEGRAM_ID])
    return CutoverManifest("cutover-pg:v1", owner, now, tuple(entries))


async def test_real_postgres_five_record_preview_atomic_apply_concurrent_replay(database):
    manifest = await seed(database)
    service = SqlAlchemyOperatorFundingCutover(database, configured_ids=[TELEGRAM_ID])
    assert all(row.status == "ready" for row in await service.preview(manifest))
    async with database() as session:
        assert (
            await session.scalar(select(func.count()).select_from(db.OperatorFundingCutover)) == 0
        )
        before = [
            (row.id, row.state, row.provider_server_id, row.last_accrued_at, row.deleted_at)
            for row in (await session.scalars(select(db.Server).order_by(db.Server.id))).all()
        ]
    outcomes = await asyncio.gather(service.apply(manifest), service.apply(manifest))
    assert {rows[0].status for rows in outcomes} == {"ready", "replayed"}
    async with database() as session:
        after = [
            (row.id, row.state, row.provider_server_id, row.last_accrued_at, row.deleted_at)
            for row in (await session.scalars(select(db.Server).order_by(db.Server.id))).all()
        ]
        assert before == after
        assert (
            await session.scalar(select(func.count()).select_from(db.OperatorFundingCutover)) == 5
        )
        assert await session.scalar(select(func.count()).select_from(db.OperatorFundingGrant)) == 0
        assert await session.scalar(select(func.count()).select_from(db.OperatorExpense)) == 0
        assert await session.scalar(select(func.count()).select_from(db.LedgerEntry)) == 0


async def test_real_postgres_audit_insert_failure_rolls_back_every_source_and_receipt(database):
    manifest = await seed(database, count=2)
    async with database.begin() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_test_cutover_audit() RETURNS trigger AS $$ "
                "BEGIN IF NEW.action='funding.server_cutover' THEN "
                "RAISE EXCEPTION 'fixture audit rejected'; END IF; RETURN NEW; "
                "END; $$ LANGUAGE plpgsql"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_test_cutover_audit BEFORE INSERT ON audit_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_test_cutover_audit()"
            )
        )
    service = SqlAlchemyOperatorFundingCutover(database, configured_ids=[TELEGRAM_ID])
    with pytest.raises(Exception, match="fixture audit rejected"):
        await service.apply(manifest)
    async with database() as session:
        assert (
            await session.scalar(select(func.count()).select_from(db.OperatorFundingCutover)) == 0
        )
        assert set((await session.scalars(select(db.Server.funding_source))).all()) == {
            "customer_wallet"
        }
