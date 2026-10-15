"""Unit coverage for encrypted restore selection, decryption and failures.

Real PostgreSQL transaction rollback and clean database round trips are
exercised in tests/live/test_postgres_backup_restore.py.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cloud_platform.backup.job import (
    BACKUP_PREFIX,
    BackupConfig,
    PostgresBackupJob,
    PostgresRestoreJob,
    RestoreError,
    list_backups,
    parse_backup_filename,
)
from cloud_platform.core.secrets import FernetSecretBox, MasterKey

DSN = "postgresql://cloud:sup3r-s3cret@db.internal:5432/clean"
DUMP = b"\x00pg_dump output CREATE TABLE servers (id uuid); -- secret rows"
KEY = MasterKey.generate()
OTHER_KEY = MasterKey.generate()
OLD = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
NEW = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)


def _backup_file(path: Path, name: str, sql: bytes = DUMP, key: MasterKey = KEY) -> Path:
    target = path / name
    target.write_bytes(FernetSecretBox(key).encrypt_bytes(sql).encode("ascii"))
    return target


def _restore_job(
    output_dir: Path,
    *,
    key: MasterKey = KEY,
    dsn: str = DSN,
    runner=None,
) -> PostgresRestoreJob:
    return PostgresRestoreJob(dsn=dsn, key=key, output_dir=output_dir, runner=runner)


def _psql_sink(sql_seen: list[bytes]) -> AsyncMock:
    """A fake psql: records the SQL it receives, exits 0."""

    async def runner(command, sql: bytes) -> subprocess.CompletedProcess:
        sql_seen.append(sql)
        return subprocess.CompletedProcess(args=list(command), returncode=0, stdout=b"", stderr=b"")

    return runner


class TestFileSelection:
    def test_parses_the_embedded_timestamp(self) -> None:
        stamp = parse_backup_filename("cloud-backup-20260820-093000.dump.enc")
        assert stamp == NEW

    def test_rejects_foreign_names(self) -> None:
        for bad in ("notes.txt", "cloud-backup-nonsense.dump.enc", "cloud-backup.dump.enc"):
            with pytest.raises(ValueError):
                parse_backup_filename(bad)

    def test_list_backups_newest_first_and_only_own_files(self) -> None:
        tmp = Path("/tmp/nonexistent-for-test")
        # no dir -> empty, no error
        assert list_backups(tmp) == []


class TestEncryptedDumpTransfer:
    """Verify encrypted backup bytes are decrypted and passed to the runner."""

    async def test_round_trip_passes_original_dump_to_runner(self, tmp_path: Path) -> None:
        # 1) a backup is produced (the M11-007 job, with a fake pg_dump)
        async def fake_pg_dump(command) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args=list(command), returncode=0, stdout=DUMP, stderr=b""
            )

        backup_dir = tmp_path / "backups"
        job = PostgresBackupJob(
            BackupConfig(dsn=DSN, output_dir=backup_dir, retention_days=14, key=KEY),
            runner=fake_pg_dump,  # type: ignore[arg-type]
        )
        result = await job.run()
        files = list(backup_dir.iterdir())
        assert len(files) == 1

        # 2) the CLEAN environment: a fresh dir holding the backup file, a
        #    separate sink standing in for psql against the clean database
        clean_dir = tmp_path / "clean"
        clean_dir.mkdir()
        (clean_dir / files[0].name).write_bytes(files[0].read_bytes())
        sql_seen: list[bytes] = []
        restore = _restore_job(clean_dir, runner=_psql_sink(sql_seen))

        # 3) restore the newest backup into the clean environment
        restored = await restore.run()

        assert restored == result.filename
        # the exact original SQL reached psql, exactly once
        assert sql_seen == [DUMP]

    async def test_latest_picks_the_newest_backup(self, tmp_path: Path) -> None:
        old_name = f"{BACKUP_PREFIX}{OLD.strftime('%Y%m%d-%H%M%S')}.dump.enc"
        new_name = f"{BACKUP_PREFIX}{NEW.strftime('%Y%m%d-%H%M%S')}.dump.enc"
        _backup_file(tmp_path, old_name, sql=b"old dump")
        _backup_file(tmp_path, new_name, sql=b"new dump")

        assert [p.name for p in list_backups(tmp_path)] == [new_name, old_name]

        sql_seen: list[bytes] = []
        restore = _restore_job(tmp_path, runner=_psql_sink(sql_seen))
        assert await restore.run() == new_name
        assert sql_seen == [b"new dump"]

    async def test_named_backup_restores_that_file(self, tmp_path: Path) -> None:
        old_name = f"{BACKUP_PREFIX}{OLD.strftime('%Y%m%d-%H%M%S')}.dump.enc"
        new_name = f"{BACKUP_PREFIX}{NEW.strftime('%Y%m%d-%H%M%S')}.dump.enc"
        _backup_file(tmp_path, old_name, sql=b"old dump")
        _backup_file(tmp_path, new_name, sql=b"new dump")

        sql_seen: list[bytes] = []
        restore = _restore_job(tmp_path, runner=_psql_sink(sql_seen))
        assert await restore.run(old_name) == old_name
        assert sql_seen == [b"old dump"]

    async def test_missing_named_backup_is_rejected(self, tmp_path: Path) -> None:
        _backup_file(tmp_path, f"{BACKUP_PREFIX}{NEW.strftime('%Y%m%d-%H%M%S')}.dump.enc")
        restore = _restore_job(tmp_path, runner=_psql_sink([]))
        with pytest.raises(RestoreError, match="not found"):
            await restore.run("cloud-backup-19990101-000000.dump.enc")

    async def test_no_backups_is_rejected(self, tmp_path: Path) -> None:
        restore = _restore_job(tmp_path, runner=_psql_sink([]))
        with pytest.raises(RestoreError, match="no backups"):
            await restore.run()


class TestWrongKey:
    """A key that does not match the backup's key fails the Fernet MAC and
    aborts BEFORE anything reaches the database."""

    async def test_wrong_key_aborts_before_psql(self, tmp_path: Path) -> None:
        name = f"{BACKUP_PREFIX}{NEW.strftime('%Y%m%d-%H%M%S')}.dump.enc"
        _backup_file(tmp_path, name, key=KEY)  # backed up with KEY
        sql_seen: list[bytes] = []
        restore = _restore_job(tmp_path, key=OTHER_KEY, runner=_psql_sink(sql_seen))

        with pytest.raises(RestoreError, match="decryption failed"):
            await restore.run()

        assert sql_seen == []  # nothing reached the clean database

    async def test_corrupted_file_aborts_before_psql(self, tmp_path: Path) -> None:
        name = f"{BACKUP_PREFIX}{NEW.strftime('%Y%m%d-%H%M%S')}.dump.enc"
        target = _backup_file(tmp_path, name)
        raw = bytearray(target.read_bytes())
        raw[len(raw) // 2] = (raw[len(raw) // 2] + 1) % 256
        target.write_bytes(bytes(raw))
        sql_seen: list[bytes] = []
        restore = _restore_job(tmp_path, runner=_psql_sink(sql_seen))

        with pytest.raises(RestoreError, match="decryption failed"):
            await restore.run()
        assert sql_seen == []


class TestPsqlFailure:
    """A psql failure (constraint error in the clean DB) is a RestoreError
    whose message is scrubbed: no DSN, no password."""

    async def test_psql_failure_is_scrubbed_and_backup_untouched(self, tmp_path: Path) -> None:
        name = f"{BACKUP_PREFIX}{NEW.strftime('%Y%m%d-%H%M%S')}.dump.enc"
        backup = _backup_file(tmp_path, name)
        before = backup.read_bytes()

        async def failing_psql(command, sql: bytes) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(
                args=list(command),
                returncode=1,
                stdout=b"",
                # psql echoes the connection: the password is in the output
                stderr=b"psql: error: connection to server failed: "
                + DSN.encode()
                + b" password rejected",
            )

        restore = _restore_job(tmp_path, runner=failing_psql)
        with pytest.raises(RestoreError) as exc:
            await restore.run()

        message = str(exc.value)
        assert "sup3r-s3cret" not in message
        assert DSN not in message
        assert "***" in message
        # the backup file is pristine for a retry
        assert backup.read_bytes() == before
        assert backup.exists()

    async def test_runner_exception_does_not_expose_dsn_in_traceback(self, tmp_path: Path) -> None:
        import traceback

        _backup_file(tmp_path, "cloud-backup-20261015-120000.dump.enc")

        async def runner(command, sql: bytes) -> subprocess.CompletedProcess:
            raise OSError(f"cannot execute {command!r}")

        with pytest.raises(RestoreError) as caught:
            await _restore_job(tmp_path, runner=runner).run()
        rendered = "".join(traceback.format_exception(caught.value))
        assert DSN not in rendered
        assert "sup3r-s3cret" not in rendered


class TestConfigValidation:
    def test_empty_dsn_rejected(self) -> None:
        with pytest.raises(ValueError, match="dsn"):
            PostgresRestoreJob(dsn="  ", key=KEY, output_dir=Path("/tmp/x"))

    def test_non_platform_files_are_invisible_to_selection(self, tmp_path: Path) -> None:
        (tmp_path / "someone-else.txt").write_text("keep me")
        (tmp_path / f"{BACKUP_PREFIX}notatime.dump.enc").write_text("unparseable")
        assert list_backups(tmp_path) == []
        restore = _restore_job(tmp_path, runner=_psql_sink([]))

        async def _run() -> None:
            with pytest.raises(RestoreError, match="no backups"):
                await restore.run()

        import asyncio

        asyncio.run(_run())
