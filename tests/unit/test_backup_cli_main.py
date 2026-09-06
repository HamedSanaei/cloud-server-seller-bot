"""Tests for ``python -m cloud_platform.backup`` (backup/__main__.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.backup.__main__ import main
from cloud_platform.backup.job import BackupError, RestoreError
from cloud_platform.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:pass@db:5432/cloud",
        backup_output_dir="C:/tmp/backups",
        backup_encryption_key="backup-test-key-not-a-real-secret",
        backup_keep=7,
    )


def _backup_result() -> MagicMock:
    result = MagicMock()
    result.filename = "cloud_20260101_000000.gpg"
    result.size_bytes = 4096
    result.deleted_files = ("old.gpg",)
    return result


class TestBackupMode:
    def test_backup_success_prints_summary(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: MagicMock(),
        )
        job = MagicMock(run=AsyncMock(return_value=_backup_result()))
        monkeypatch.setattr("cloud_platform.backup.__main__.PostgresBackupJob", lambda cfg: job)
        assert main([]) == 0
        out = capsys.readouterr().out
        assert "backup ok: cloud_20260101_000000.gpg (4096 bytes), pruned 1 expired" in out

    def test_backup_config_error_returns_1(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: (_ for _ in ()).throw(BackupError("no key configured")),
        )
        assert main([]) == 1
        assert "backup failed: no key configured" in capsys.readouterr().out

    def test_backup_run_error_returns_1(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: MagicMock(),
        )
        job = MagicMock(run=AsyncMock(side_effect=BackupError("pgdump missing")))
        monkeypatch.setattr("cloud_platform.backup.__main__.PostgresBackupJob", lambda cfg: job)
        assert main([]) == 1
        assert "backup failed: pgdump missing" in capsys.readouterr().out


class TestRestoreMode:
    def _restore_config(self) -> MagicMock:
        config = MagicMock()
        config.key = "k"
        config.output_dir = MagicMock()
        return config

    def test_restore_latest_success(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: self._restore_config(),
        )
        job = MagicMock(run=AsyncMock(return_value="cloud_20260101_000000.gpg"))
        monkeypatch.setattr("cloud_platform.backup.__main__.PostgresRestoreJob", lambda **k: job)
        assert main(["--restore"]) == 0
        assert "restore ok:" in capsys.readouterr().out
        assert job.run.await_args.args == (None,)

    def test_restore_named_file(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: self._restore_config(),
        )
        job = MagicMock(run=AsyncMock(return_value="x"))
        monkeypatch.setattr("cloud_platform.backup.__main__.PostgresRestoreJob", lambda **k: job)
        assert main(["--restore", "cloud_20260102_000000.gpg"]) == 0
        assert job.run.await_args.args == ("cloud_20260102_000000.gpg",)

    def test_restore_converts_asyncpg_dsn_and_target_override(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: self._restore_config(),
        )
        captured: dict[str, object] = {}
        job = MagicMock(run=AsyncMock(return_value="x"))

        def fake_job(**kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return job

        monkeypatch.setattr("cloud_platform.backup.__main__.PostgresRestoreJob", fake_job)
        assert main(["--restore", "--target-dsn", "postgresql://other/db"]) == 0
        assert captured["dsn"] == "postgresql://other/db"

        # without target-dsn the asyncpg scheme is converted
        assert main(["--restore"]) == 0
        assert captured["dsn"] == "postgresql://user:pass@db:5432/cloud"

    def test_restore_config_error_returns_1(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: (_ for _ in ()).throw(BackupError("bad key")),
        )
        assert main(["--restore"]) == 1
        assert "restore failed: bad key" in capsys.readouterr().out

    def test_restore_run_error_returns_1(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr("cloud_platform.backup.__main__.get_settings", lambda: settings)
        monkeypatch.setattr(
            "cloud_platform.backup.__main__.build_backup_config",
            lambda s: self._restore_config(),
        )
        job = MagicMock(run=AsyncMock(side_effect=RestoreError("pg_restore failed")))
        monkeypatch.setattr("cloud_platform.backup.__main__.PostgresRestoreJob", lambda **k: job)
        assert main(["--restore"]) == 1
        assert "restore failed: pg_restore failed" in capsys.readouterr().out
