"""Opt-in admin-status transaction tests on the dedicated scratch PostgreSQL.

Set CLOUD_PLATFORM_TEST_POSTGRES_URL to a postgresql+asyncpg scratch URL on
loopback. Each test owns a random schema containing only users/audit_events;
no migrations, shared-table truncations, or application URL fallback are allowed.
The schema is dropped even when an assertion or a database statement fails.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import MetaData, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DefaultClause

from cloud_platform.db.base import AuditEvent as AuditRow
from cloud_platform.db.base import BusinessLogEvent as BusinessLogRow
from cloud_platform.db.base import User as UserRow
from cloud_platform.modules.users.domain import UserNotFound, UserStatus
from cloud_platform.modules.users.repository import (
    SqlAlchemyUserRepository,
    UserStatusIdempotencyConflict,
)

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DB_URL,
    reason="requires an explicit scratch CLOUD_PLATFORM_TEST_POSTGRES_URL on loopback",
)


@pytest.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("admin-status live tests only accept scratch PostgreSQL on loopback")
    schema = f"admin_status_test_{uuid4().hex}"
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
    for model in (UserRow, AuditRow, BusinessLogRow):
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


async def seed_user(
    database: async_sessionmaker[AsyncSession],
    *,
    status: UserStatus = UserStatus.FROZEN,
    admin: bool = False,
) -> UUID:
    user_id = uuid4()
    async with database.begin() as session:
        session.add(
            UserRow(
                id=user_id,
                username=f"user-{user_id.hex}",
                email=f"{user_id.hex}@example.test",
                role="admin" if admin else "user",
                status=status.value,
            )
        )
    return user_id


async def persisted_status(database: async_sessionmaker[AsyncSession], user_id: UUID) -> str:
    async with database() as session:
        return str(
            (
                await session.execute(select(UserRow.status).where(UserRow.id == user_id))
            ).scalar_one()
        )


async def audit_count(database: async_sessionmaker[AsyncSession]) -> int:
    async with database() as session:
        return int((await session.execute(select(func.count()).select_from(AuditRow))).scalar_one())


async def test_activation_replay_returns_snapshot_without_undoing_later_ban(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    target_id = await seed_user(database)
    repo = SqlAlchemyUserRepository(database)
    first = await repo.change_status_idempotently(
        target_id, UserStatus.ACTIVE, actor_id=actor_id, idempotency_key="K", reason="reviewed"
    )
    await repo.change_status_idempotently(
        target_id, UserStatus.BANNED, actor_id=actor_id, idempotency_key="L", reason="fraud"
    )
    replay = await repo.change_status_idempotently(
        target_id, UserStatus.ACTIVE, actor_id=actor_id, idempotency_key="K", reason="reviewed"
    )
    assert first is replay is UserStatus.ACTIVE
    assert await persisted_status(database, target_id) == "banned"
    assert await audit_count(database) == 2
    async with database() as session:
        receipt = (
            await session.execute(
                select(AuditRow).where(AuditRow.event_metadata["idempotency_key"].astext == "K")
            )
        ).scalar_one()
        assert receipt.actor_type == "admin"
        assert receipt.actor_id == actor_id
        assert receipt.action == "admin.user.status_changed"
        assert receipt.resource_type == "user"
        assert receipt.resource_id == str(target_id)
        assert receipt.reason == "reviewed"
        assert receipt.event_metadata == {"status": "active", "idempotency_key": "K"}


@pytest.mark.parametrize("different_fact", ["target", "missing_target", "status", "reason"])
async def test_actor_key_rejects_changed_command_facts(
    database: async_sessionmaker[AsyncSession], different_fact: str
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    target_id = await seed_user(database)
    other_id = await seed_user(database)
    repo = SqlAlchemyUserRepository(database)
    await repo.change_status_idempotently(
        target_id, UserStatus.ACTIVE, actor_id=actor_id, idempotency_key="K", reason="reviewed"
    )
    changed_target = target_id
    if different_fact == "target":
        changed_target = other_id
    elif different_fact == "missing_target":
        changed_target = uuid4()
    with pytest.raises(UserStatusIdempotencyConflict):
        await repo.change_status_idempotently(
            changed_target,
            UserStatus.BANNED if different_fact == "status" else UserStatus.ACTIVE,
            actor_id=actor_id,
            idempotency_key="K",
            reason="different reason" if different_fact == "reason" else "reviewed",
        )
    assert await persisted_status(database, target_id) == "active"
    assert await persisted_status(database, other_id) == "frozen"
    assert await audit_count(database) == 1


async def test_same_key_is_independent_for_different_actors(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    second_actor = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    target_id = await seed_user(database)
    repo = SqlAlchemyUserRepository(database)
    await repo.change_status_idempotently(
        target_id, UserStatus.ACTIVE, actor_id=actor_id, idempotency_key="K", reason="reviewed"
    )
    await repo.change_status_idempotently(
        target_id, UserStatus.BANNED, actor_id=second_actor, idempotency_key="K", reason="fraud"
    )
    assert await persisted_status(database, target_id) == "banned"
    assert await audit_count(database) == 2


async def test_failed_audit_commit_rolls_back_status_and_receipt(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    target_id = await seed_user(database)
    # Deferred rejection occurs only at commit, after both writes have flushed.
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
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                "EXECUTE FUNCTION reject_test_audit()"
            )
        )
    repo = SqlAlchemyUserRepository(database)
    with pytest.raises(DBAPIError, match="audit rejected for test"):
        await repo.change_status_idempotently(
            target_id, UserStatus.ACTIVE, actor_id=actor_id, idempotency_key="K", reason="reviewed"
        )
    assert await persisted_status(database, target_id) == "frozen"
    assert await audit_count(database) == 0


async def test_concurrent_same_key_replays_never_reapply(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    target_id = await seed_user(database)
    repo = SqlAlchemyUserRepository(database)

    async def activate() -> UserStatus:
        return await repo.change_status_idempotently(
            target_id, UserStatus.ACTIVE, actor_id=actor_id, idempotency_key="K", reason="reviewed"
        )

    assert await asyncio.gather(*(activate() for _ in range(8))) == [UserStatus.ACTIVE] * 8
    assert await audit_count(database) == 1
    await repo.change_status_idempotently(
        target_id, UserStatus.BANNED, actor_id=actor_id, idempotency_key="L", reason="fraud"
    )
    assert await asyncio.gather(*(activate() for _ in range(8))) == [UserStatus.ACTIVE] * 8
    assert await persisted_status(database, target_id) == "banned"
    assert await audit_count(database) == 2


async def test_concurrent_key_reuse_across_targets_has_only_one_winner(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    targets = [await seed_user(database), await seed_user(database)]
    repo = SqlAlchemyUserRepository(database)
    outcomes = await asyncio.gather(
        *(
            repo.change_status_idempotently(
                target_id,
                UserStatus.ACTIVE,
                actor_id=actor_id,
                idempotency_key="K",
                reason="reviewed",
            )
            for target_id in targets
        ),
        return_exceptions=True,
    )
    assert sum(outcome is UserStatus.ACTIVE for outcome in outcomes) == 1
    assert sum(isinstance(outcome, UserStatusIdempotencyConflict) for outcome in outcomes) == 1
    assert sorted([await persisted_status(database, target_id) for target_id in targets]) == [
        "active",
        "frozen",
    ]
    assert await audit_count(database) == 1


async def test_actor_row_is_not_locked_by_status_command(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    target_id = await seed_user(database)
    repo = SqlAlchemyUserRepository(database)
    async with database.begin() as session:
        await session.execute(select(UserRow).where(UserRow.id == actor_id).with_for_update())
        result = await asyncio.wait_for(
            repo.change_status_idempotently(
                target_id,
                UserStatus.ACTIVE,
                actor_id=actor_id,
                idempotency_key="K",
                reason="reviewed",
            ),
            timeout=3,
        )
    assert result is UserStatus.ACTIVE
    assert await audit_count(database) == 1


async def test_missing_target_does_not_consume_key(
    database: async_sessionmaker[AsyncSession],
) -> None:
    actor_id = await seed_user(database, admin=True, status=UserStatus.ACTIVE)
    target_id = uuid4()
    repo = SqlAlchemyUserRepository(database)
    with pytest.raises(UserNotFound):
        await repo.change_status_idempotently(
            target_id, UserStatus.ACTIVE, actor_id=actor_id, idempotency_key="K", reason="reviewed"
        )
    assert await audit_count(database) == 0
