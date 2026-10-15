"""Opt-in token revocation races against real, migrated scratch PostgreSQL.

Requires CLOUD_PLATFORM_TEST_POSTGRES_URL. Creates and drops a unique database
on that test server; never truncates or changes the configured database.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cloud_platform.db.base import ApiTokenRow, User
from cloud_platform.modules.tokens.domain import ApiToken, InvalidTokenError, TokenScope, hash_token
from cloud_platform.modules.tokens.repository import SqlAlchemyApiTokenRepository
from cloud_platform.modules.tokens.service import TokenService

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not DB_URL, reason="requires scratch PostgreSQL test URL")


@pytest.fixture(scope="module")
def token_database_url() -> Iterator[str]:
    if not DB_URL:
        pytest.skip("requires scratch PostgreSQL test URL")
    name = f"cloud_platform_token_{uuid4().hex[:12]}"

    async def change_database(create: bool) -> None:
        engine = create_async_engine(DB_URL, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                verb = "CREATE DATABASE" if create else "DROP DATABASE"
                suffix = "" if create else " WITH (FORCE)"
                await connection.execute(text(f'{verb} "{name}"{suffix}'))
        finally:
            await engine.dispose()

    asyncio.run(change_database(True))
    url = make_url(DB_URL).set(database=name).render_as_string(hide_password=False)
    try:
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[2],
            env=dict(os.environ, DATABASE_URL=url),
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, "scratch token schema migration failed"
        yield url
    finally:
        asyncio.run(change_database(False))


@pytest.fixture
async def token_sessions(
    token_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(token_database_url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def create_owner(sessions: async_sessionmaker[AsyncSession]) -> UUID:
    user_id = uuid4()
    async with sessions() as session:
        session.add(
            User(
                id=user_id,
                username=f"token-owner-{user_id.hex}",
                email=f"{user_id.hex}@example.test",
            )
        )
        await session.commit()
    return user_id


class PausedTokenReadRepository(SqlAlchemyApiTokenRepository):
    """Pause after the real hash lookup, before authentication can save it."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(sessions)
        self.read_complete = asyncio.Event()
        self.resume = asyncio.Event()

    async def get_by_hash(self, token_hash: str) -> ApiToken | None:
        snapshot = await super().get_by_hash(token_hash)
        self.read_complete.set()
        await self.resume.wait()
        return snapshot


async def test_committed_revocation_defeats_stale_authentication_save(
    token_sessions: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_owner(token_sessions)
    repository = SqlAlchemyApiTokenRepository(token_sessions)
    service = TokenService(repository)
    token, raw = await service.create(
        actor_user_id=owner,
        owner_user_id=owner,
        name="interleaved-auth",
        scopes=frozenset({TokenScope.SERVERS_READ}),
    )
    assert token.id is not None
    # Scoped authentication is still usable before revocation.
    auth = await service.authenticate(raw)
    assert auth.user_id == owner
    assert auth.scopes == {TokenScope.SERVERS_READ}
    before = await repository.get(token.id)
    assert before is not None and before.last_used_at is not None

    paused_repository = PausedTokenReadRepository(token_sessions)
    stale_authentication = asyncio.create_task(TokenService(paused_repository).authenticate(raw))
    try:
        await asyncio.wait_for(paused_repository.read_complete.wait(), timeout=10)
        # This separate connection commits while authentication holds its stale
        # unrevoked domain snapshot. No sleep or scheduler-dependent ordering.
        revoked = await asyncio.wait_for(
            service.revoke(actor_user_id=owner, token_id=token.id), timeout=10
        )
        assert revoked.revoked_at is not None
        paused_repository.resume.set()
        with pytest.raises(InvalidTokenError, match="invalid or revoked token"):
            await asyncio.wait_for(stale_authentication, timeout=10)
    finally:
        paused_repository.resume.set()
        await asyncio.gather(stale_authentication, return_exceptions=True)

    stored = await repository.get(token.id)
    assert stored is not None
    assert stored.revoked_at == revoked.revoked_at
    assert stored.last_used_at == before.last_used_at
    assert stored.scopes == {TokenScope.SERVERS_READ}
    with pytest.raises(InvalidTokenError, match="invalid or revoked token"):
        await service.authenticate(raw)


async def test_save_preserves_issued_facts_and_monotonic_lifecycle(
    token_sessions: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_owner(token_sessions)
    repository = SqlAlchemyApiTokenRepository(token_sessions)
    service = TokenService(repository)
    token, raw = await service.create(
        actor_user_id=owner,
        owner_user_id=owner,
        name="immutable-facts",
        scopes=frozenset({TokenScope.WALLET_READ}),
    )
    assert token.id is not None
    first_use = datetime.now(UTC)
    latest_use = first_use + timedelta(seconds=1)
    await repository.save(replace(token, last_used_at=latest_use))

    # A stale caller cannot change credentials, ownership, scopes or metadata,
    # nor move the last-use clock backwards.
    forged = replace(
        token,
        user_id=uuid4(),
        name="changed-name",
        token_hash=hash_token("cpt_forged-credential"),
        prefix="cpt_forged",
        scopes=frozenset({TokenScope.ADMIN_READ, TokenScope.ADMIN_WRITE}),
        created_at=first_use - timedelta(days=1),
        last_used_at=first_use,
    )
    saved = await repository.save(forged)
    assert saved == replace(token, last_used_at=latest_use)
    auth = await service.authenticate(raw)
    assert auth.user_id == owner
    assert auth.scopes == {TokenScope.WALLET_READ}

    revoked = await service.revoke(actor_user_id=owner, token_id=token.id)
    assert revoked.revoked_at is not None
    replay = await repository.save(
        replace(forged, revoked_at=revoked.revoked_at + timedelta(seconds=1))
    )
    assert replay == revoked
    stale = await repository.save(token)
    assert stale == revoked
    with pytest.raises(InvalidTokenError):
        await service.authenticate(raw)


async def test_disappearing_token_is_denied_instead_of_recreated(
    token_sessions: async_sessionmaker[AsyncSession],
) -> None:
    owner = await create_owner(token_sessions)
    repository = SqlAlchemyApiTokenRepository(token_sessions)
    service = TokenService(repository)
    token, raw = await service.create(
        actor_user_id=owner,
        owner_user_id=owner,
        name="deleted-during-auth",
        scopes=frozenset({TokenScope.CATALOG_READ}),
    )
    assert token.id is not None
    paused_repository = PausedTokenReadRepository(token_sessions)
    authentication = asyncio.create_task(TokenService(paused_repository).authenticate(raw))
    try:
        await asyncio.wait_for(paused_repository.read_complete.wait(), timeout=10)
        async with token_sessions() as session:
            await session.execute(delete(ApiTokenRow).where(ApiTokenRow.id == token.id))
            await session.commit()
        paused_repository.resume.set()
        with pytest.raises(InvalidTokenError, match="invalid or revoked token"):
            await asyncio.wait_for(authentication, timeout=10)
    finally:
        paused_repository.resume.set()
        await asyncio.gather(authentication, return_exceptions=True)
    assert await repository.get(token.id) is None
