"""Run one encrypted PostgreSQL backup: ``python -m cloud_platform.backup``.

Schedule this with the platform's job runner (cron / CI / arq); it prints a
one-line summary that is safe to log (no DSN, no key, no passwords).

Restore (see docs/operations/RUNBOOK.md "Restore drill"):
``python -m cloud_platform.backup --restore`` (newest backup) or
``--restore FILENAME`` for a specific one; ``--target-dsn`` overrides the
configured database URL for restores into a clean environment.
"""

from __future__ import annotations

import argparse
import asyncio

from cloud_platform.backup.job import (
    BackupError,
    PostgresBackupJob,
    PostgresRestoreJob,
    RestoreError,
    build_backup_config,
)
from cloud_platform.core.config import Settings, get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloud_platform.backup")
    parser.add_argument("--restore", nargs="?", const="latest", metavar="FILENAME", default=None)
    parser.add_argument(
        "--target-dsn",
        default=None,
        help="DSN to restore into (defaults to the configured database URL)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()

    if args.restore is not None:
        return _run_restore(settings, args.restore, args.target_dsn)
    return _run_backup(settings)


def _run_backup(settings: Settings) -> int:
    try:
        config = build_backup_config(settings)
    except BackupError as exc:
        print(f"backup failed: {exc}")
        return 1
    job = PostgresBackupJob(config)
    try:
        result = asyncio.run(job.run())
    except BackupError as exc:
        print(f"backup failed: {exc}")
        return 1
    print(
        f"backup ok: {result.filename} ({result.size_bytes} bytes), "
        f"pruned {len(result.deleted_files)} expired"
    )
    return 0


def _run_restore(settings: Settings, filename: str, target_dsn: str | None) -> int:
    if target_dsn:
        dsn = target_dsn
    else:
        dsn = settings.database_url
        if dsn.startswith("postgresql+asyncpg://"):
            dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    try:
        config = build_backup_config(settings)  # also validates the restore key
    except BackupError as exc:
        print(f"restore failed: {exc}")
        return 1
    job = PostgresRestoreJob(
        dsn=dsn,
        key=config.key,
        output_dir=config.output_dir,
    )
    try:
        restored = asyncio.run(job.run(filename if filename != "latest" else None))
    except RestoreError as exc:
        print(f"restore failed: {exc}")
        return 1
    print(f"restore ok: {restored}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
