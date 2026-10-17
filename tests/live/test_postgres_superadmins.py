"""Opt-in Super Admin transactions against isolated loopback PostgreSQL schemas.

Only CLOUD_PLATFORM_TEST_POSTGRES_URL is accepted; application/production URLs
are never used. Explicit opt-in setup failures fail the tests rather than skip.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import MetaData, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DefaultClause

from cloud_platform.db.base import AuditEvent as AuditRow
from cloud_platform.db.base import BusinessLogEvent as BusinessLogRow
from cloud_platform.db.base import LedgerEntry as LedgerRow
from cloud_platform.db.base import TelegramSuperadminAssignment as AssignmentRow
from cloud_platform.db.base import User as UserRow
from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository
from cloud_platform.modules.users.superadmin_repository import (
    SUPERADMIN_ADVISORY_LOCK_KEY,
    SqlAlchemySuperadminRepository,
    apply_configured_superadmin,
    authorize_superadmin_transaction,
    lock_superadmin_reconciliation,
)

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="requires explicit scratch CLOUD_PLATFORM_TEST_POSTGRES_URL on loopback"
)


@pytest.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Super Admin live tests only accept scratch PostgreSQL on loopback")
    schema = f"superadmin_test_{uuid4().hex}"
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
    subset = MetaData()
    for model in (UserRow, WalletRow, LedgerRow, AuditRow, AssignmentRow, BusinessLogRow):
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
            await connection.run_sync(subset.create_all)
            await connection.execute(
                text(
                    "CREATE FUNCTION immutable_test_history() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'history is immutable'; END; $$"
                )
            )
            for table_name in ("audit_events", "ledger"):
                await connection.execute(
                    text(
                        f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table_name} "
                        "FOR EACH ROW EXECUTE FUNCTION immutable_test_history()"
                    )
                )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()


async def seed_user(
    database: async_sessionmaker[AsyncSession],
    telegram_id: int | None,
    *,
    role: str = "user",
    status: str = "frozen",
) -> UUID:
    user_id, wallet_id = uuid4(), uuid4()
    async with database.begin() as session:
        session.add(
            UserRow(
                id=user_id,
                username=f"u-{user_id.hex}",
                email=f"{user_id.hex}@example.test",
                telegram_user_id=telegram_id,
                role=role,
                status=status,
                terms_version=7,
                phone_number="+989121234567",
                national_id="1234567890",
            )
        )
        await session.flush()
        session.add(WalletRow(id=wallet_id, user_id=user_id, balance=123456, currency="IRT"))
        await session.flush()
        session.add(
            LedgerRow(
                id=uuid4(),
                wallet_id=wallet_id,
                entry_type="deposit",
                amount=123456,
                currency="IRT",
                idempotency_key=f"seed-{user_id.hex}",
                description="Existing immutable customer transaction",
            )
        )
    return user_id


async def snapshot(database: async_sessionmaker[AsyncSession]) -> dict[str, list[dict[str, Any]]]:
    async with database() as session:
        return {
            name: [
                dict(row)
                for row in (
                    await session.execute(text(f"SELECT * FROM {name} ORDER BY {primary_key}"))
                ).mappings()
            ]
            for name, primary_key in (
                ("users", "id"),
                ("wallets", "id"),
                ("ledger", "id"),
                ("audit_events", "id"),
                ("telegram_superadmin_assignments", "telegram_user_id"),
            )
        }


async def test_existing_accounts_granted_without_altering_financial_or_identity_history(
    database: async_sessionmaker[AsyncSession],
) -> None:
    ids = [123456789, 987654321]
    user_ids = [await seed_user(database, value) for value in ids]
    unrelated = await seed_user(database, 555555555, role="admin", status="banned")
    before = await snapshot(database)
    repo = SqlAlchemySuperadminRepository(database)
    await repo.reconcile(ids)
    after = await snapshot(database)
    assert after["wallets"] == before["wallets"]
    assert after["ledger"] == before["ledger"]
    for previous in before["users"]:
        actual = next(row for row in after["users"] if row["id"] == previous["id"])
        if previous["id"] == unrelated:
            assert actual == previous
        else:
            assert previous["id"] in user_ids
            assert actual["role"] == "admin" and actual["status"] == "active"
            assert {
                k: v for k, v in actual.items() if k not in {"role", "status", "updated_at"}
            } == {k: v for k, v in previous.items() if k not in {"role", "status", "updated_at"}}
    assert len(after["audit_events"]) == 4
    assert all(row["actor_type"] == "system" for row in after["audit_events"])
    assert all(row["actor_id"] is None for row in after["audit_events"])
    await repo.reconcile(list(reversed(ids)) + ids)
    assert await snapshot(database) == after


async def test_concurrent_startups_and_pending_onboarding_grant_once(
    database: async_sessionmaker[AsyncSession],
) -> None:
    repo = SqlAlchemySuperadminRepository(database)
    await asyncio.gather(*(repo.reconcile([123456789]) for _ in range(8)))
    pending = await snapshot(database)
    assert pending["users"] == []
    assert len(pending["audit_events"]) == 1
    assert pending["telegram_superadmin_assignments"][0]["user_id"] is None
    user_id = await seed_user(database, 123456789)

    async def onboard() -> bool:
        async with database.begin() as session:
            await lock_superadmin_reconciliation(session)
            user = (
                await session.execute(select(UserRow).where(UserRow.id == user_id))
            ).scalar_one()
            return await apply_configured_superadmin(session, user, onboarding=True)

    results = await asyncio.gather(*(onboard() for _ in range(8)))
    assert sum(results) == 1
    final = await snapshot(database)
    assert len(final["users"]) == 1
    assert final["users"][0]["id"] == user_id
    assert final["users"][0]["role"] == "admin"
    assert final["users"][0]["status"] == "active"
    assert len(final["audit_events"]) == 2


async def test_removal_revokes_bound_admin_preserves_status_and_prevents_stale_onboarding_grant(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await seed_user(database, 123456789)
    repo = SqlAlchemySuperadminRepository(database)
    await repo.reconcile([123456789, 987654321])
    async with database.begin() as session:
        await session.execute(
            text("UPDATE users SET status = 'banned' WHERE id = :id"), {"id": user_id}
        )
    before = await snapshot(database)
    await repo.reconcile([])
    after = await snapshot(database)
    assert after["users"][0]["role"] == "user"
    assert after["users"][0]["status"] == "banned"
    assert after["wallets"] == before["wallets"] and after["ledger"] == before["ledger"]
    assert all(not assignment["enabled"] for assignment in after["telegram_superadmin_assignments"])
    assert len(after["audit_events"]) == len(before["audit_events"]) + 3
    async with database.begin() as session:
        user = (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one()
        assert not await apply_configured_superadmin(session, user)
    await repo.reconcile([])
    assert await snapshot(database) == after
    # Re-adding the ID is an explicit, audited policy change, not a stale request.
    await repo.reconcile([123456789])
    restored = await snapshot(database)
    assert restored["users"][0]["role"] == "admin"
    assert restored["users"][0]["status"] == "active"


async def test_startup_repairs_configured_privileges_but_not_other_accounts(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await seed_user(database, 123456789)
    repo = SqlAlchemySuperadminRepository(database)
    await repo.reconcile([123456789])
    async with database.begin() as session:
        await session.execute(
            text("UPDATE users SET role = 'user', status = 'frozen' WHERE id = :id"),
            {"id": user_id},
        )
    before = await snapshot(database)
    await repo.reconcile([123456789])
    after = await snapshot(database)
    assert after["users"][0]["role"] == "admin" and after["users"][0]["status"] == "active"
    assert len(after["audit_events"]) == len(before["audit_events"]) + 1


async def test_failed_audit_commit_rolls_back_assignments_and_user_privileges(
    database: async_sessionmaker[AsyncSession],
) -> None:
    await seed_user(database, 123456789)
    before = await snapshot(database)
    async with database.begin() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_test_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'audit rejected for test'; END; $$"
            )
        )
        await session.execute(
            text(
                "CREATE CONSTRAINT TRIGGER reject_test_audit AFTER INSERT ON audit_events "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reject_test_audit()"
            )
        )
    with pytest.raises(DBAPIError, match="audit rejected for test"):
        await SqlAlchemySuperadminRepository(database).reconcile([123456789, 987654321])
    assert await snapshot(database) == before


async def test_real_advisory_lock_uses_signed_bigint_and_serializes_onboarding(
    database: async_sessionmaker[AsyncSession],
) -> None:
    assert -(2**63) <= SUPERADMIN_ADVISORY_LOCK_KEY <= 2**63 - 1
    user_id = await seed_user(database, 123456789)
    await SqlAlchemySuperadminRepository(database).reconcile([123456789])
    async with database.begin() as session:
        await lock_superadmin_reconciliation(session)
        task = asyncio.create_task(SqlAlchemySuperadminRepository(database).reconcile([]))
        await asyncio.sleep(0.15)
        assert not task.done()
        user = (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one()
        assert not await apply_configured_superadmin(session, user)
    await asyncio.wait_for(task, timeout=3)
    assert (await snapshot(database))["users"][0]["role"] == "user"


async def test_no_telegram_identity_and_revoked_pending_identity_never_grant(
    database: async_sessionmaker[AsyncSession],
) -> None:
    anonymous_id = await seed_user(database, None)
    repo = SqlAlchemySuperadminRepository(database)
    await repo.reconcile([123456789])
    await repo.reconcile([])
    pending_id = await seed_user(database, 123456789)
    async with database.begin() as session:
        for user_id in (anonymous_id, pending_id):
            user = (
                await session.execute(select(UserRow).where(UserRow.id == user_id))
            ).scalar_one()
            assert not await apply_configured_superadmin(session, user)
    final = await snapshot(database)
    assert all(user["role"] == "user" for user in final["users"])
    assert len(final["audit_events"]) == 2


async def test_privileged_transaction_linearizes_before_revocation_then_fails_closed(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await seed_user(database, 123456789)
    repo = SqlAlchemySuperadminRepository(database)
    await repo.reconcile([123456789])
    async with database.begin() as session:
        await authorize_superadmin_transaction(session, user_id, [123456789])
        revocation = asyncio.create_task(repo.reconcile([]))
        await asyncio.sleep(0.15)
        assert not revocation.done()
        # This represents a command wholly preceding the revocation; the same
        # transaction may mutate a wallet only after the fresh RBAC check.
        role = (
            await session.execute(select(UserRow.role).where(UserRow.id == user_id))
        ).scalar_one()
        assert role == "admin"
    await asyncio.wait_for(revocation, timeout=3)
    async with database.begin() as session:
        # An old process's local IDs cannot bypass the committed revoked role.
        with pytest.raises(PermissionDeniedError):
            await authorize_superadmin_transaction(session, user_id, [123456789])


async def test_already_bound_onboarding_cannot_undo_runtime_freeze_or_role_revocation(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await seed_user(database, 123456789)
    repo = SqlAlchemySuperadminRepository(database)
    await repo.reconcile([123456789])
    async with database.begin() as session:
        await session.execute(
            text("UPDATE users SET role = 'user', status = 'frozen' WHERE id = :id"),
            {"id": user_id},
        )
    before = await snapshot(database)
    async with database.begin() as session:
        await lock_superadmin_reconciliation(session)
        user = (await session.execute(select(UserRow).where(UserRow.id == user_id))).scalar_one()
        assert not await apply_configured_superadmin(session, user, onboarding=True)
    assert await snapshot(database) == before
    # Explicit startup reconciliation, not per-request onboarding, repairs the
    # configuration-owned desired role and activation state.
    await repo.reconcile([123456789])
    final = await snapshot(database)
    assert final["users"][0]["role"] == "admin" and final["users"][0]["status"] == "active"


async def test_real_trusted_onboarding_concurrently_creates_one_account_wallet_and_grant(
    database: async_sessionmaker[AsyncSession],
) -> None:
    await SqlAlchemySuperadminRepository(database).reconcile([123456789])
    users = SqlAlchemyUserRepository(database)
    results = await asyncio.gather(
        *(
            users.onboard_telegram(123456789, "verified-admin", "123456789@telegram.test")
            for _ in range(8)
        )
    )
    assert len({user.id for user in results}) == 1
    assert all(user.role.value == "admin" and user.status.value == "active" for user in results)
    after = await snapshot(database)
    assert len(after["users"]) == len(after["wallets"]) == 1
    assert after["wallets"][0]["balance"] == 0
    assert len(after["audit_events"]) == 2
    assert after["telegram_superadmin_assignments"][0]["user_id"] == results[0].id


async def test_real_trusted_onboarding_preserves_existing_wallet_and_never_links_by_username(
    database: async_sessionmaker[AsyncSession],
) -> None:
    original_id = await seed_user(database, 123456789, status="active")
    before = await snapshot(database)
    original_username = before["users"][0]["username"]
    users = SqlAlchemyUserRepository(database)
    existing = await users.onboard_telegram(
        123456789, "changed-username", "different@telegram.test"
    )
    assert existing.id == original_id
    assert await snapshot(database) == before
    # Even knowing an existing user's username cannot claim its identity,
    # wallet or privileges. The authenticated Telegram sender is the key.
    newcomer = await users.onboard_telegram(987654321, original_username, "987654321@telegram.test")
    assert newcomer.id != original_id
    assert newcomer.username != original_username
    after = await snapshot(database)
    original_wallet = before["wallets"][0]
    assert original_wallet in after["wallets"]
    assert after["ledger"] == before["ledger"]


async def test_removed_assignment_denies_stale_configuration_even_if_role_manually_restored(
    database: async_sessionmaker[AsyncSession],
) -> None:
    user_id = await seed_user(database, 123456789, status="active")
    repo = SqlAlchemySuperadminRepository(database)
    await repo.reconcile([123456789])
    await repo.reconcile([])
    async with database.begin() as session:
        await session.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :id"), {"id": user_id}
        )
    before = await snapshot(database)
    async with database.begin() as session:
        with pytest.raises(PermissionDeniedError, match="enabled bound"):
            await authorize_superadmin_transaction(session, user_id, [123456789])
    assert await snapshot(database) == before


@pytest.mark.parametrize("assignment_state", ["absent", "unbound", "other_user"])
async def test_unmanaged_pending_or_wrongly_bound_assignment_cannot_authorize(
    database: async_sessionmaker[AsyncSession], assignment_state: str
) -> None:
    repo = SqlAlchemySuperadminRepository(database)
    if assignment_state != "absent":
        await repo.reconcile([123456789])
    user_id = await seed_user(database, 123456789, status="active", role="admin")
    if assignment_state == "other_user":
        other_user_id = await seed_user(database, 987654321, status="active")
        async with database.begin() as session:
            await session.execute(
                text(
                    "UPDATE telegram_superadmin_assignments SET user_id = :id "
                    "WHERE telegram_user_id = 123456789"
                ),
                {"id": other_user_id},
            )
    before = await snapshot(database)
    async with database.begin() as session:
        with pytest.raises(PermissionDeniedError, match="enabled bound"):
            await authorize_superadmin_transaction(session, user_id, [123456789])
    assert await snapshot(database) == before
