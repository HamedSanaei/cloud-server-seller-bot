"""Run one encrypted PostgreSQL backup: ``python -m cloud_platform.backup``.

Schedule this with the platform's job runner (cron / CI / arq); it prints a
one-line summary that is safe to log (no DSN, no key, no passwords).
"""

from __future__ import annotations

import asyncio

from cloud_platform.backup.job import BackupError, PostgresBackupJob, build_backup_config
from cloud_platform.core.config import get_settings


def main() -> int:
    settings = get_settings()
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


if __name__ == "__main__":
    raise SystemExit(main())
