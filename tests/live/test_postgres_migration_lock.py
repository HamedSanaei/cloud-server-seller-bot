"""Actual Alembic DDL excludes another writer and releases after commit/failure.

Only the explicit scratch PostgreSQL URL is accepted. A unique schema is removed
on exit; no application tables or production configuration are consulted.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or sys.platform != "linux",
    reason="requires Linux and an explicit scratch PostgreSQL database",
)


def _migration_writer(schema: str, fail: bool) -> int:
    from alembic.config import Config
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    def ddl(_revisions, context):
        connection = context.bind
        connection.execute(sa.text(f'CREATE TABLE "{schema}".canary (id integer PRIMARY KEY)'))
        connection.execute(sa.text(f'INSERT INTO "{schema}".canary VALUES (1)'))
        print("migration-in-flight", flush=True)
        if sys.stdin.readline().strip() != "release":
            raise RuntimeError("migration test controller disappeared")
        if fail:
            connection.execute(sa.text(f'INSERT INTO "{schema}".canary VALUES (1)'))
        return []

    try:
        with EnvironmentContext(config, scripts, fn=ddl, version_table_schema=schema):
            scripts.run_env()
    except sa.exc.IntegrityError as error:
        if fail and error.orig.pgcode == "23505":
            return 17
        raise
    return 0


@pytest.mark.parametrize("migration_fails", [False, True])
def test_real_migration_excludes_ddl_writer_until_commit_or_rollback(
    tmp_path: Path, migration_fails: bool
) -> None:
    engine = sa.create_engine(
        TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://"),
        hide_parameters=True,
    )
    schema = "migration_lock_" + uuid4().hex
    process = None
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        process = subprocess.Popen(
            [sys.executable, __file__, schema, "failure" if migration_fails else "success"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=tmp_path,
            env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
        )
        assert process.stdout is not None
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(timeout=30), "migration did not reach its DDL transaction"
        assert process.stdout.readline().strip() == "migration-in-flight"
        with engine.connect() as contender:
            assert not contender.scalar(sa.text("SELECT pg_try_advisory_xact_lock(739180054)"))
            assert contender.scalar(sa.text(f"SELECT to_regclass('{schema}.canary')")) is None
            contender.rollback()
            stdout, stderr = process.communicate(input="release\n", timeout=30)
            assert process.returncode == (17 if migration_fails else 0), stdout + stderr
            assert contender.scalar(sa.text("SELECT pg_try_advisory_xact_lock(739180054)"))
            exists = contender.scalar(sa.text(f"SELECT to_regclass('{schema}.canary')"))
            if migration_fails:
                assert exists is None, "failed DDL was committed"
            else:
                assert contender.scalar(sa.text(f'SELECT id FROM "{schema}".canary')) == 1
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=10)
        with engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(_migration_writer(sys.argv[1], sys.argv[2] == "failure"))
