"""Opt-in PostgreSQL migration/locking contract; never an application DB."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DefaultClause

from cloud_platform.db.base import (
    AuditEvent,
    LedgerEntry,
    Provider,
    ProviderAccount,
    Server,
    ServerAccessGrant,
    TelegramSuperadminAssignment,
    User,
    Wallet,
)
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
from cloud_platform.modules.servers.delegation_repository import ServerAccessRepository
from cloud_platform.modules.servers.test_identity import TEST_IPV4, TEST_PROVIDER_ID, is_test_server

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="explicit scratch loopback PostgreSQL not configured"
)


@pytest.fixture
async def database():
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("only explicit scratch PostgreSQL on loopback is allowed")
    schema = f"access_grants_test_{uuid4().hex}"
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "5000",
                "timezone": "UTC",
            }
        },
    )
    metadata, pending, seen = (
        MetaData(),
        [
            m.__table__
            for m in (
                ServerAccessGrant,
                AuditEvent,
                TelegramSuperadminAssignment,
                Wallet,
                LedgerEntry,
            )
        ],
        set(),
    )
    while pending:
        table = pending.pop()
        if table.name in seen:
            continue
        seen.add(table.name)
        pending.extend(fk.column.table for fk in table.foreign_keys)
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if column.server_default is not None:
                if "uuid_generate_v4" in str(column.server_default.arg):
                    column.server_default = DefaultClause(text("gen_random_uuid()"))
                elif str(column.server_default.arg).upper() == "CURRENT_TIMESTAMP":
                    column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    created = False
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
            await conn.run_sync(
                lambda sync: metadata.create_all(
                    sync,
                    tables=[t for t in metadata.sorted_tables if t.name != "server_access_grants"],
                )
            )

            def migrate(sync):
                path = Path(__file__).parents[2] / "alembic/versions/0061_server_access_grants.py"
                spec = importlib.util.spec_from_file_location("access_migration", path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                with Operations.context(MigrationContext.configure(sync)):
                    module.upgrade()

            await conn.run_sync(migrate)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            if created:
                async with engine.begin() as conn:
                    await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()


async def seed(database):
    owner, delegate, provider, account, server = [uuid4() for _ in range(5)]
    async with database.begin() as session:
        session.add_all(
            [
                User(
                    id=owner,
                    username="operator",
                    email="operator@example.test",
                    role="admin",
                    status="active",
                    telegram_user_id=42,
                ),
                User(
                    id=delegate,
                    username="linked",
                    email="linked@example.test",
                    role="user",
                    status="active",
                    telegram_user_id=99,
                ),
                Provider(id=provider, name="leaseweb", region="global"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                TelegramSuperadminAssignment(telegram_user_id=42, user_id=owner, enabled=True),
                ProviderAccount(id=account, provider_id=provider, user_id=owner, status="active"),
                Wallet(id=uuid4(), user_id=delegate, balance=123456, currency="IRT"),
            ]
        )
        await session.flush()
        session.add(
            Server(
                id=server,
                user_id=owner,
                provider_id=provider,
                provider_account_id=account,
                credential_account_id="default",
                provider_server_id=TEST_PROVIDER_ID,
                ipv4=TEST_IPV4,
                state="running",
                billing_model="prepaid_monthly_fixed",
                funding_source="operator",
                funding_operator_id=owner,
                currency="IRT",
                price_per_quantum=0,
            )
        )
    return owner, delegate, server


async def test_real_migration_concurrent_replay_and_revocation_do_not_change_money(database):
    owner, delegate, server_id = await seed(database)
    repo = ServerAccessRepository(database)
    args = dict(
        actor_id=owner,
        server_id=server_id,
        user_id=delegate,
        operations=["view"],
        reason="Explicit fixture delegation",
        key="fixture-grant",
        configured_ids=[42],
    )
    results = await asyncio.gather(repo.change(**args), repo.change(**args))
    assert sorted(result["replayed"] for result in results) == [False, True]
    async with database() as session:
        assert len((await session.execute(select(ServerAccessGrant))).scalars().all()) == 1
        assert len((await session.execute(select(AuditEvent))).scalars().all()) == 1
    server = await SqlAlchemyServerRepository(database).get(server_id)
    assert (
        is_test_server(server) and server.user_id == owner and server.funding_operator_id == owner
    )
    assert await repo.permissions(delegate, server) == {"view"}
    assert await repo.server_ids(delegate) == [server_id]
    await repo.change(**{**args, "key": "fixture-revoke", "operations": [], "revoke": True})
    assert not (await repo.change(**args))["active"]  # old replay never re-grants
    assert not await repo.permissions(delegate, server) and not await repo.server_ids(delegate)
    async with database() as session:
        ownership = (
            await session.execute(
                select(
                    Server.user_id,
                    Server.funding_source,
                    Server.funding_operator_id,
                    Server.price_per_quantum,
                    Server.currency,
                ).where(Server.id == server_id)
            )
        ).one()
        assert tuple(ownership) == (owner, "operator", owner, 0, "IRT")
        assert (
            await session.execute(select(Wallet.balance).where(Wallet.user_id == delegate))
        ).scalar_one() == 123456
        assert (await session.execute(select(LedgerEntry))).scalars().all() == []
        assert len((await session.execute(select(AuditEvent))).scalars().all()) == 2
