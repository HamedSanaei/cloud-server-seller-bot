"""Opt-in encrypted backup/restore drills on isolated scratch databases.

Only CLOUD_PLATFORM_TEST_POSTGRES_URL is used. The supplied scratch role must
be able to create databases; explicit service/client failures fail, never skip.
Requires pg_dump and psql matching the scratch PostgreSQL server version.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest

from cloud_platform.backup.job import (
    BackupConfig,
    PostgresBackupJob,
    PostgresRestoreJob,
    RestoreError,
)
from cloud_platform.core.secrets import FernetSecretBox, MasterKey

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="requires CLOUD_PLATFORM_TEST_POSTGRES_URL (scratch PostgreSQL)"
)


@pytest.fixture
async def databases() -> AsyncIterator[tuple[str, str]]:
    dsn = DB_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        admin = await asyncpg.connect(dsn, timeout=10)
    except Exception as exc:
        pytest.fail(f"scratch PostgreSQL connection failed: {type(exc).__name__}", pytrace=False)
    names = [f"backup_drill_{uuid4().hex}" for _ in range(2)]
    created: list[str] = []
    try:
        for name in names:
            await admin.execute(f'CREATE DATABASE "{name}"')
            created.append(name)
        parts = urlsplit(dsn)
        yield tuple(
            urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))
            for name in names
        )
    finally:
        for name in reversed(created):
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def test_encrypted_backup_restores_schema_and_rows_to_fresh_database(
    databases: tuple[str, str], tmp_path: Path
) -> None:
    source, target = databases
    connection = await asyncpg.connect(source)
    try:
        await connection.execute(
            "CREATE SCHEMA drill; "
            "CREATE TABLE drill.records (id integer PRIMARY KEY, value text NOT NULL); "
            "INSERT INTO drill.records VALUES (1, 'first'), (2, 'second');"
        )
    finally:
        await connection.close()
    key = MasterKey.generate()
    result = await PostgresBackupJob(
        BackupConfig(dsn=source, output_dir=tmp_path, retention_days=1, key=key)
    ).run()
    artifact = tmp_path / result.filename
    encrypted = artifact.read_bytes()
    assert b"CREATE TABLE" not in encrypted
    assert b"CREATE TABLE drill.records" in FernetSecretBox(key).decrypt_bytes(
        encrypted.decode("ascii")
    )
    connection = await asyncpg.connect(target)
    try:
        assert await connection.fetchval("SELECT to_regclass('drill.records')") is None
    finally:
        await connection.close()
    restored = await PostgresRestoreJob(dsn=target, key=key, output_dir=tmp_path).run()
    assert restored == result.filename
    assert artifact.read_bytes() == encrypted
    connection = await asyncpg.connect(target)
    try:
        rows = await connection.fetch("SELECT id, value FROM drill.records ORDER BY id")
        assert [tuple(row) for row in rows] == [(1, "first"), (2, "second")]
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE table_schema = 'drill' AND table_name = 'records' "
                "AND constraint_type = 'PRIMARY KEY'"
            )
            == 1
        )
    finally:
        await connection.close()


async def test_sql_error_raises_and_rolls_back_every_write(
    databases: tuple[str, str], tmp_path: Path
) -> None:
    _, target = databases
    connection = await asyncpg.connect(target)
    try:
        await connection.execute(
            "CREATE TABLE restore_existing (id integer PRIMARY KEY); "
            "INSERT INTO restore_existing VALUES (1)"
        )
    finally:
        await connection.close()
    key = MasterKey.generate()
    artifact = tmp_path / "cloud-backup-20261015-120000.dump.enc"
    sql = (
        b"INSERT INTO restore_existing VALUES (2);\n"
        b"CREATE TABLE restore_partial (id integer);\n"
        b"INSERT INTO restore_existing VALUES (1);\n"
        b"INSERT INTO restore_existing VALUES (3);\n"
    )
    artifact.write_bytes(FernetSecretBox(key).encrypt_bytes(sql).encode("ascii"))
    before = artifact.read_bytes()
    with pytest.raises(RestoreError, match="psql failed") as caught:
        await PostgresRestoreJob(dsn=target, key=key, output_dir=tmp_path).run()
    assert target not in str(caught.value)
    assert artifact.read_bytes() == before
    connection = await asyncpg.connect(target)
    try:
        assert await connection.fetchval("SELECT array_agg(id) FROM restore_existing") == [1]
        assert await connection.fetchval("SELECT to_regclass('restore_partial')") is None
    finally:
        await connection.close()
