"""Opt-in owner-only encrypted delivery against a migrated scratch PostgreSQL DB.

Requires CLOUD_PLATFORM_TEST_POSTGRES_URL. Never modifies that configured DB;
creates and drops a unique scratch database on the same test server.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cloud_platform.core.secrets import FernetSecretBox, MasterKey
from cloud_platform.db.base import Provider, ProviderAccount, Server, ServerCreateCredential, User
from cloud_platform.modules.hourly.credentials import SqlAlchemyServerCredentialStore

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not DB_URL, reason="requires scratch PostgreSQL test URL")


@contextmanager
def scratch_database() -> Iterator[str]:
    name = f"cloud_platform_credential_{uuid4().hex[:12]}"

    async def change_database(create: bool) -> None:
        engine = create_async_engine(DB_URL, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as connection:
                verb = "CREATE DATABASE" if create else "DROP DATABASE"
                suffix = "" if create else " WITH (FORCE)"
                await connection.execute(sa.text(f'{verb} "{name}"{suffix}'))
        finally:
            await engine.dispose()

    asyncio.run(change_database(True))
    url = make_url(DB_URL).set(database=name).render_as_string(hide_password=False)
    try:
        environment = dict(os.environ, DATABASE_URL=url)
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, "scratch credential schema migration failed"
        yield url
    finally:
        asyncio.run(change_database(False))


@pytest.fixture
def scratch_url() -> Iterator[str]:
    if not DB_URL:
        pytest.skip("requires scratch PostgreSQL test URL")
    with scratch_database() as url:
        yield url


@pytest.mark.asyncio
async def test_encrypted_claim_ack_owner_and_crash_retry(scratch_url: str) -> None:
    engine = create_async_engine(scratch_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    box = FernetSecretBox(MasterKey.generate())
    store = SqlAlchemyServerCredentialStore(factory, box=box)
    owner, intruder, server_id, provider_id, account_id = (uuid4() for _ in range(5))
    provider_server_id = "provider-created-1"
    password = secrets.token_urlsafe(40)
    try:
        async with factory() as session:
            session.add_all(
                [
                    User(
                        id=owner, username=f"owner-{owner.hex}", email=f"{owner.hex}@example.test"
                    ),
                    User(
                        id=intruder,
                        username=f"intruder-{intruder.hex}",
                        email=f"{intruder.hex}@example.test",
                    ),
                    Provider(id=provider_id, name=f"test-{provider_id.hex}"),
                    ProviderAccount(id=account_id, provider_id=provider_id, user_id=owner),
                    Server(
                        id=server_id,
                        user_id=owner,
                        provider_id=provider_id,
                        provider_account_id=account_id,
                        state="requested",
                        price_per_quantum=1,
                        currency="USD",
                    ),
                ]
            )
            await session.commit()

        await store.save_issued(
            server_id=server_id,
            provider_server_id=provider_server_id,
            password=password,
            username="root",
        )
        async with factory() as session:
            row = await session.get(ServerCreateCredential, server_id)
            assert row is not None
            assert password not in repr(row.__dict__)
            assert row.ciphertext != password
        assert not await store.has_for_owner(server_id=server_id, user_id=owner)
        assert await store.claim_for_owner(server_id=server_id, user_id=owner) is None
        async with factory() as session:
            server = await session.get(Server, server_id)
            assert server is not None
            server.provider_server_id = provider_server_id
            await session.commit()
        assert not await store.has_for_owner(server_id=server_id, user_id=intruder)
        assert await store.claim_for_owner(server_id=server_id, user_id=intruder) is None
        assert await store.has_for_owner(server_id=server_id, user_id=owner)

        claim = await store.claim_for_owner(server_id=server_id, user_id=owner)
        assert claim is not None
        assert claim.username == "root"
        assert password not in repr(claim)
        assert await store.claim_for_owner(server_id=server_id, user_id=owner) is None
        assert claim.reveal() == password
        assert claim.reveal() is None
        assert not await store.ack_for_owner(
            server_id=server_id, user_id=intruder, claim_id=claim.claim_id
        )
        assert await store.release_for_owner(
            server_id=server_id, user_id=owner, claim_id=claim.claim_id
        )
        retry = await store.claim_for_owner(server_id=server_id, user_id=owner)
        assert retry is not None and retry.reveal() == password
        assert not await store.ack_for_owner(
            server_id=server_id, user_id=owner, claim_id=claim.claim_id
        )
        assert await store.ack_for_owner(
            server_id=server_id, user_id=owner, claim_id=retry.claim_id
        )
        assert not await store.has_for_owner(server_id=server_id, user_id=owner)
        assert await store.claim_for_owner(server_id=server_id, user_id=owner) is None
        async with factory() as session:
            row = await session.get(ServerCreateCredential, server_id)
            assert row is not None and row.ciphertext is None
        with pytest.raises(RuntimeError, match="already exists"):
            await store.save_issued(
                server_id=server_id,
                provider_server_id=provider_server_id,
                password=password,
            )
        rotated = secrets.token_urlsafe(40)
        await store.replace_issued(
            server_id=server_id,
            provider_server_id=provider_server_id,
            password=rotated,
            username="root",
        )
        assert await store.claim_for_owner(server_id=server_id, user_id=intruder) is None
        rotated_claim = await store.claim_for_owner(server_id=server_id, user_id=owner)
        assert rotated_claim is not None and rotated_claim.reveal() == rotated
        await store.invalidate_for_owner(
            server_id=server_id, user_id=owner, provider_server_id=provider_server_id
        )
        assert not await store.has_for_owner(server_id=server_id, user_id=owner)
        assert not await store.ack_for_owner(
            server_id=server_id, user_id=owner, claim_id=rotated_claim.claim_id
        )
    finally:
        await engine.dispose()
