"""PostgreSQL backup automation (M11-007).

Encrypted ``pg_dump`` backups with retention: each run dumps the database,
encrypts the dump with Fernet under the configured master key, writes it next
to its siblings in the output directory, and prunes backups older than the
retention window. The DSN and key never appear in logs or error messages.
"""

from cloud_platform.backup.job import (
    BackupConfig,
    BackupError,
    BackupResult,
    PostgresBackupJob,
    build_backup_config,
    build_pg_dump_command,
    redact_dsn,
)

__all__ = [
    "BackupConfig",
    "BackupError",
    "BackupResult",
    "PostgresBackupJob",
    "build_backup_config",
    "build_pg_dump_command",
    "redact_dsn",
]
