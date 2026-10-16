"""Opt-in funding checks using the actual financial migration and PostgreSQL.

Only an explicitly supplied scratch loopback CLOUD_PLATFORM_TEST_POSTGRES_URL
is accepted. The application configuration and its database are never loaded.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, ForeignKey, MetaData, Table, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DefaultClause

from cloud_platform.db.base import AuditEvent, TelegramSuperadminAssignment, User
from cloud_platform.modules.funding import (
    FundingAuthorizationError,
    FundingFactMismatchError,
    FundingIdempotencyConflictError,
    FundingPurpose,
    FundingStateError,
    ProviderCostSnapshot,
    SqlAlchemyOperatorFundingRepository,
    expense_operation_key,
)
from cloud_platform.modules.funding.repository import _lock_funding_policy
from cloud_platform.modules.users.superadmin_repository import (
    SqlAlchemySuperadminRepository,
    lock_superadmin_reconciliation,
)

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not DB_URL, reason="explicit scratch loopback PostgreSQL required")
TEST_TELEGRAM_ID = 10101


def _migration():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0056_operator_funding.py"
    spec = importlib.util.spec_from_file_location("funding_migration_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("funding tests require explicit scratch PostgreSQL on loopback")
    schema = f"funding_test_{uuid4().hex}"
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "application_name": schema,
                "statement_timeout": "8000",
                "lock_timeout": "2500",
                "timezone": "UTC",
            }
        },
    )
    predecessor = MetaData()
    for model in (User, AuditEvent, TelegramSuperadminAssignment):
        table = model.__table__.to_metadata(predecessor)
        for column in table.columns:
            default = column.server_default
            if default is not None and "uuid_generate_v4" in str(default.arg):
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif default is not None and str(default.arg) == "CURRENT_TIMESTAMP":
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    Table(
        "servers",
        predecessor,
        Column("id", PG_UUID, primary_key=True),
        Column("user_id", PG_UUID, ForeignKey("users.id"), nullable=False),
    )

    def migrate(connection) -> None:
        predecessor.create_all(connection)
        # The real 0056 migration expects the predecessor's UUID helper.
        connection.execute(
            text(
                "CREATE FUNCTION uuid_generate_v4() RETURNS uuid "
                "LANGUAGE SQL AS $$ SELECT gen_random_uuid() $$"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            _migration().upgrade()

    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(migrate)
        created = True
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()


async def seed(database) -> UUID:
    owner = uuid4()
    async with database.begin() as session:
        session.add(
            User(
                id=owner,
                username=f"funding-{owner.hex}",
                email=f"{owner.hex}@example.test",
                telegram_user_id=TEST_TELEGRAM_ID,
                role="user",
                status="active",
            )
        )
    await SqlAlchemySuperadminRepository(database).reconcile([TEST_TELEGRAM_ID])
    return owner


async def reserve(repo, owner, key="funding:period:1", amount=120):
    return await repo.reserve(
        operator_id=owner,
        owner_id=owner,
        operation_key=key,
        resource_key="server:period:1",
        purpose=FundingPurpose.HOURLY_USAGE,
        nominal_amount_minor=amount,
        nominal_currency="IRT",
        provider_cost_snapshot=ProviderCostSnapshot(
            "0.1234567890123456789012345678901234", "EUR", "provider quote"
        ),
        reason="Authorize isolated operator funding test period",
    )


async def counts(database) -> tuple[int, int, int]:
    async with database() as session:
        return tuple(
            [
                int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
                for table in ("operator_funding_grants", "operator_expenses", "audit_events")
            ]
        )


async def test_concurrent_maximum_key_reserve_settle_read_and_replay(database) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    key = "x" * 200
    grants = await asyncio.wait_for(
        asyncio.gather(*(reserve(repo, owner, key) for _ in range(4))), 10
    )
    assert len({grant.id for grant in grants}) == 1
    expenses = await asyncio.wait_for(
        asyncio.gather(*(repo.settle(grants[0]) for _ in range(4))), 10
    )
    assert len({expense.id for expense in expenses}) == 1
    assert await repo.get_expense(expense_operation_key(key)) == expenses[0]
    assert await repo.settle(grants[0]) == expenses[0]
    assert (await counts(database))[:2] == (1, 1)
    with pytest.raises(FundingIdempotencyConflictError):
        await reserve(repo, owner, key, amount=121)
    with pytest.raises(FundingStateError):
        await repo.cancel(grants[0], reason="already accepted")


async def test_reserve_versus_policy_revocation_cutover_lock_has_no_deadlock(database) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    task = None
    try:
        async with database.begin() as policy_session:
            await lock_superadmin_reconciliation(policy_session)
            task = asyncio.create_task(reserve(repo, owner))
            # Observe a real policy-lock waiter before attempting the funding
            # lock. Inverted reserve order holds funding and deadlocks here.
            async with asyncio.timeout(2):
                while True:
                    await policy_session.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = (
                        await policy_session.execute(
                            text(
                                "SELECT count(*) FROM pg_locks l "
                                "JOIN pg_stat_activity a ON a.pid=l.pid "
                                "WHERE l.locktype='advisory' AND NOT l.granted "
                                "AND a.application_name=current_setting('application_name')"
                            )
                        )
                    ).scalar_one()
                    if waiting:
                        break
                    await asyncio.sleep(0.01)
            await _lock_funding_policy(policy_session)
            await policy_session.execute(
                text("UPDATE telegram_superadmin_assignments SET enabled=false")
            )
            await policy_session.execute(
                text("UPDATE users SET role='user' WHERE id=:id"), {"id": owner}
            )
        with pytest.raises(FundingAuthorizationError):
            await asyncio.wait_for(task, 4)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert (await counts(database))[:2] == (0, 0)


async def test_revocation_blocks_new_period_but_accepted_grant_can_settle(database) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    grant = await reserve(repo, owner)
    await SqlAlchemySuperadminRepository(database).reconcile([])
    assert not await repo.eligible(owner)
    with pytest.raises(FundingAuthorizationError):
        await reserve(repo, owner, "new-period")
    expense = await repo.settle(grant)
    assert expense.grant_id == grant.id


@pytest.mark.parametrize("policy", ["unconfigured", "frozen", "role_revoked", "unbound"])
async def test_new_grants_require_fresh_configured_active_bound_policy(database, policy) -> None:
    owner = await seed(database)
    configured = [] if policy == "unconfigured" else [TEST_TELEGRAM_ID]
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=configured)
    async with database.begin() as session:
        if policy == "frozen":
            await session.execute(
                text("UPDATE users SET status='frozen' WHERE id=:id"), {"id": owner}
            )
        elif policy == "role_revoked":
            await session.execute(text("UPDATE users SET role='user' WHERE id=:id"), {"id": owner})
        elif policy == "unbound":
            await session.execute(text("UPDATE telegram_superadmin_assignments SET user_id=NULL"))
    assert not await repo.eligible(owner)
    with pytest.raises(FundingAuthorizationError):
        await reserve(repo, owner)
    assert (await counts(database))[:2] == (0, 0)


@pytest.mark.parametrize("operation", ["reserve", "settle", "cancel"])
async def test_failed_audit_constraint_rolls_back_every_funding_mutation(
    database, operation
) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    grant = await reserve(repo, owner) if operation != "reserve" else None
    before = await counts(database)
    async with database.begin() as session:
        # Raise an actual integrity-class SQLSTATE at COMMIT, reproducing the
        # former broad IntegrityError handler rather than only a generic error.
        await session.execute(
            text(
                "CREATE FUNCTION reject_funding_audit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'funding audit rejected' "
                "USING ERRCODE='23514'; END; $$"
            )
        )
        await session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER reject_funding_audit "
                "AFTER INSERT ON audit_events DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION reject_funding_audit()"
            )
        )
    with pytest.raises(DBAPIError, match="funding audit rejected"):
        if operation == "reserve":
            await reserve(repo, owner)
        elif operation == "settle":
            await repo.settle(grant)
        else:
            await repo.cancel(grant, reason="provider did not accept")
    assert await counts(database) == before
    if grant is not None:
        assert (await repo.get_grant(grant.operation_key)).state.value == "authorized"


async def test_actual_migration_expense_cutover_triggers_restrict_and_noop_downgrade(
    database,
) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    grant = await reserve(repo, owner)
    expense = await repo.settle(grant)
    server_id, cutover_id = uuid4(), uuid4()
    async with database.begin() as session:
        await session.execute(
            text("INSERT INTO servers(id,user_id) VALUES (:id,:owner)"),
            {"id": server_id, "owner": owner},
        )
        await session.execute(
            text(
                "INSERT INTO operator_funding_cutovers "
                "(id,operation_key,server_id,operator_user_id,prior_funding_source,"
                "effective_at,reason,metadata) "
                "VALUES (:id,'cutover:1',:server,:owner,'customer_wallet',"
                "now(),'explicit test','{}')"
            ),
            {"id": cutover_id, "server": server_id, "owner": owner},
        )
    for table, identifier, assignment in (
        ("operator_expenses", expense.id, "nominal_amount_minor=999"),
        ("operator_funding_cutovers", cutover_id, "reason='rewritten'"),
    ):
        for sql in (
            f"UPDATE {table} SET {assignment} WHERE id=:id",
            f"DELETE FROM {table} WHERE id=:id",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                async with database.begin() as session:
                    await session.execute(text(sql), {"id": identifier})
    for table, identifier in (
        ("users", owner),
        ("servers", server_id),
        ("operator_funding_grants", grant.id),
    ):
        with pytest.raises(DBAPIError):
            async with database.begin() as session:
                await session.execute(text(f"DELETE FROM {table} WHERE id=:id"), {"id": identifier})
    before = await counts(database)
    async with database.begin() as session:
        connection = await session.connection()

        def downgrade(sync_connection) -> None:
            with Operations.context(MigrationContext.configure(sync_connection)):
                _migration().downgrade()

        await connection.run_sync(downgrade)
    assert await counts(database) == before


async def test_settled_replay_detects_corrupt_expense_facts(database) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    grant = await reserve(repo, owner)
    # Simulate independently inserted, structurally valid but incoherent
    # accepted evidence; no immutability trigger is disabled to create it.
    async with database.begin() as session:
        await session.execute(
            text(
                "INSERT INTO operator_expenses "
                "(id,grant_id,operation_key,owner_user_id,operator_user_id,resource_key,purpose,"
                "nominal_amount_minor,nominal_currency,provider_cost_snapshot,occurred_at) "
                "SELECT :id,id,:key,owner_user_id,operator_user_id,resource_key,purpose,"
                "nominal_amount_minor+1,nominal_currency,provider_cost_snapshot,now() "
                "FROM operator_funding_grants WHERE id=:grant"
            ),
            {"id": uuid4(), "grant": grant.id, "key": expense_operation_key(grant.operation_key)},
        )
        await session.execute(
            text("UPDATE operator_funding_grants SET state='settled' WHERE id=:id"),
            {"id": grant.id},
        )
    with pytest.raises(FundingFactMismatchError):
        await repo.settle(grant)


async def test_actual_migration_grant_financial_facts_and_history_are_immutable(database) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    grant = await reserve(repo, owner)
    updates = (
        "id=gen_random_uuid()",
        "operation_key=operation_key || '-rewritten'",
        "operator_user_id=gen_random_uuid()",
        "owner_user_id=gen_random_uuid()",
        "resource_key='rewritten-resource'",
        "purpose='monthly_renew'",
        "nominal_amount_minor=nominal_amount_minor+1",
        "nominal_currency='USD'",
        "provider_cost_snapshot=jsonb_set(provider_cost_snapshot,'{amount_exact}','\"999\"')",
        "created_at=created_at + interval '1 second'",
    )
    for assignment in updates:
        with pytest.raises(DBAPIError, match="immutable"):
            async with database.begin() as session:
                await session.execute(
                    text(f"UPDATE operator_funding_grants SET {assignment} WHERE id=:id"),
                    {"id": grant.id},
                )
    with pytest.raises(DBAPIError, match="immutable"):
        async with database.begin() as session:
            await session.execute(
                text("DELETE FROM operator_funding_grants WHERE id=:id"), {"id": grant.id}
            )
    assert await repo.get_grant(grant.operation_key) == grant
    async with database() as session:
        reason = (
            await session.execute(
                text(
                    "SELECT reason FROM audit_events "
                    "WHERE action='operator_funding.grant.reserve' AND resource_id=:id"
                ),
                {"id": str(grant.id)},
            )
        ).scalar_one()
    assert reason == "Authorize isolated operator funding test period"


@pytest.mark.parametrize("terminal_state", ["settled", "cancelled"])
async def test_actual_migration_grant_terminal_state_cannot_reopen_or_switch(
    database, terminal_state
) -> None:
    owner = await seed(database)
    repo = SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    grant = await reserve(repo, owner)
    if terminal_state == "settled":
        await repo.settle(grant)
    else:
        await repo.cancel(grant, reason="Provider rejected before acceptance")
    for invalid_state in {"authorized", "settled", "cancelled"} - {terminal_state}:
        with pytest.raises(DBAPIError, match="immutable"):
            async with database.begin() as session:
                await session.execute(
                    text("UPDATE operator_funding_grants SET state=:state WHERE id=:id"),
                    {"state": invalid_state, "id": grant.id},
                )
    async with database.begin() as session:
        await session.execute(
            text("UPDATE operator_funding_grants SET state=state WHERE id=:id"), {"id": grant.id}
        )
    assert (await repo.get_grant(grant.operation_key)).state.value == terminal_state
