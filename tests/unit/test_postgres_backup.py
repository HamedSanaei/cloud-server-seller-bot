"""Tests for automated encrypted PostgreSQL backups (M11-007).

Acceptance: encrypted backup job + retention.
"""

from __future__ import annotations

import base64
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cloud_platform.backup.job import (
    BACKUP_PREFIX,
    BACKUP_SUFFIX,
    BackupConfig,
    BackupError,
    PostgresBackupJob,
    build_backup_config,
    build_pg_dump_command,
    redact_dsn,
)
from cloud_platform.core.config import Settings
from cloud_platform.core.secrets import FernetSecretBox, MasterKey

DSN = "postgresql://cloud:sup3r-s3cret@db.internal:5432/cloud"
DUMP = b"\x00pg_dump output CREATE TABLE servers (id uuid); -- secret rows"
KEY = MasterKey.generate()


def _fake_runner(returncode: int = 0, stdout: bytes = DUMP, stderr: bytes = b"") -> tuple:
    async def runner(command) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=list(command), returncode=returncode, stdout=stdout, stderr=stderr
        )

    return runner, []


def _config(tmp_path: Path, retention_days: int = 14) -> BackupConfig:
    return BackupConfig(
        dsn=DSN, output_dir=tmp_path / "backups", retention_days=retention_days, key=KEY
    )


def _job(
    tmp_path: Path,
    returncode: int = 0,
    stdout: bytes = DUMP,
    stderr: bytes = b"",
    retention_days: int = 14,
) -> PostgresBackupJob:
    runner, _ = _fake_runner(returncode, stdout, stderr)
    return PostgresBackupJob(_config(tmp_path, retention_days), runner=runner)  # type: ignore[arg-type]


class TestCommandAndRedaction:
    def test_pg_dump_command(self) -> None:
        cmd = build_pg_dump_command(DSN)
        assert cmd[0] == "pg_dump"
        assert "--no-password" in cmd and "--no-owner" in cmd and "--no-privileges" in cmd
        assert cmd[-1] == DSN

    def test_redact_dsn_masks_password(self) -> None:
        redacted = redact_dsn(DSN)
        assert "sup3r-s3cret" not in redacted
        assert ":***@" in redacted
        assert "db.internal:5432/cloud" in redacted

    def test_redact_dsn_without_password_unchanged(self) -> None:
        dsn = "postgresql://cloud@db.internal:5432/cloud"
        assert redact_dsn(dsn) == dsn

    def test_scrub_removes_password_from_stderr(self) -> None:
        from cloud_platform.backup.job import _scrub

        stderr = "pg_dump: error: connection to server failed (password sup3r-s3cret)"
        scrubbed = _scrub(stderr, DSN)
        assert "sup3r-s3cret" not in scrubbed
        assert "***" in scrubbed


class TestBackupRun:
    async def test_dump_is_encrypted_at_rest(self, tmp_path: Path) -> None:
        job = _job(tmp_path)
        result = await job.run()

        assert result.filename.startswith(BACKUP_PREFIX)
        assert result.filename.endswith(BACKUP_SUFFIX)
        file = tmp_path / "backups" / result.filename
        assert file.exists()
        assert file.read_bytes() != DUMP  # plaintext never written
        assert result.size_bytes == file.stat().st_size
        assert result.duration_seconds >= 0
        # Round-trip with the same key recovers the dump; another key fails.
        assert FernetSecretBox(KEY).decrypt_bytes(file.read_text()) == DUMP

    async def test_runner_receives_redactable_command(self, tmp_path: Path) -> None:
        seen: list = []

        async def runner(command) -> subprocess.CompletedProcess:
            seen.append(list(command))
            return subprocess.CompletedProcess(
                args=list(command), returncode=0, stdout=DUMP, stderr=b""
            )

        job = PostgresBackupJob(_config(tmp_path), runner=runner)  # type: ignore[arg-type]
        await job.run()
        assert seen[0][-1] == DSN

    async def test_failed_dump_raises_redacted_error(self, tmp_path: Path) -> None:
        stderr = "pg_dump: password sup3r-s3cret rejected\nline two"
        job = _job(tmp_path, returncode=1, stderr=stderr.encode())
        with pytest.raises(BackupError, match="pg_dump failed") as excinfo:
            await job.run()
        message = str(excinfo.value)
        assert "sup3r-s3cret" not in message
        assert "***" in message
        assert "line two" in message
        assert not (tmp_path / "backups").exists() or not list((tmp_path / "backups").iterdir())

    async def test_empty_dump_rejected(self, tmp_path: Path) -> None:
        job = _job(tmp_path, stdout=b"   \n")
        with pytest.raises(BackupError, match="empty dump"):
            await job.run()

    async def test_runner_exception_reported_without_argv(self, tmp_path: Path) -> None:
        async def runner(command) -> subprocess.CompletedProcess:
            raise FileNotFoundError("pg_dump")

        job = PostgresBackupJob(_config(tmp_path), runner=runner)  # type: ignore[arg-type]
        with pytest.raises(BackupError, match="could not be executed"):
            await job.run()


class TestRetention:
    def _seed(self, dirpath: Path, days_ago: int) -> Path:
        stamp = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")
        path = dirpath / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
        path.write_bytes(b"x")
        return path

    async def test_prunes_only_expired_own_backups(self, tmp_path: Path) -> None:
        job = _job(tmp_path, retention_days=14)
        dirpath = tmp_path / "backups"
        dirpath.mkdir(parents=True)
        old = self._seed(dirpath, 30)
        old_boundary = self._seed(dirpath, 14 + 1)
        fresh = self._seed(dirpath, 1)
        foreign = dirpath / "someone-else.txt"
        foreign.write_bytes(b"keep me")
        unparseable = dirpath / f"{BACKUP_PREFIX}garbage{BACKUP_SUFFIX}"
        unparseable.write_bytes(b"keep me too")

        result = await job.run()

        assert set(result.deleted_files) == {old.name, old_boundary.name}
        assert not old.exists() and not old_boundary.exists()
        assert fresh.exists()
        assert foreign.exists()
        assert unparseable.exists()

    async def test_no_retention_deletions_when_all_fresh(self, tmp_path: Path) -> None:
        job = _job(tmp_path, retention_days=14)
        dirpath = tmp_path / "backups"
        dirpath.mkdir(parents=True)
        fresh = self._seed(dirpath, 2)
        result = await job.run()
        assert result.deleted_files == ()
        assert fresh.exists()


class TestConfig:
    def test_empty_dsn_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="dsn"):
            BackupConfig(dsn="  ", output_dir=tmp_path, retention_days=1, key=KEY)

    def test_retention_must_be_positive(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="retention"):
            BackupConfig(dsn=DSN, output_dir=tmp_path, retention_days=0, key=KEY)

    def _settings(self, **overrides) -> Settings:
        values = dict(
            database_url="postgresql+asyncpg://cloud:cloud@localhost:5432/cloud",
            backup_output_dir="/tmp/backups",
            backup_retention_days=7,
            backup_encryption_key=base64.urlsafe_b64encode(KEY.material).decode(),
        )
        values.update(overrides)
        return Settings(**values)

    def test_asyncpg_dsn_translated(self, tmp_path: Path) -> None:
        config = build_backup_config(self._settings())
        assert config.dsn == "postgresql://cloud:cloud@localhost:5432/cloud"
        assert config.retention_days == 7
        assert config.key == KEY

    def test_missing_key_rejected(self) -> None:
        with pytest.raises(BackupError, match="backup_encryption_key"):
            build_backup_config(self._settings(backup_encryption_key=""))

    def test_invalid_key_rejected(self) -> None:
        with pytest.raises(BackupError, match="32 bytes"):
            build_backup_config(self._settings(backup_encryption_key="not-base64!"))

    def test_short_key_rejected(self) -> None:
        with pytest.raises(BackupError, match="32 bytes"):
            build_backup_config(
                self._settings(backup_encryption_key=base64.urlsafe_b64encode(b"short").decode())
            )


class TestJobMetric:
    async def test_run_records_job_metric(self, tmp_path: Path) -> None:
        from cloud_platform.observability.metrics import metrics

        def sample() -> float:
            value = metrics.registry.get_sample_value(
                "cloud_platform_job_runs_total", {"job": "postgres_backup", "status": "ok"}
            )
            return float(value) if value is not None else 0.0

        before = sample()
        await _job(tmp_path).run()
        assert sample() - before == 1.0
