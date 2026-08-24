"""Encrypted pg_dump backup job with retention (M11-007).

Security properties:
- The dump is encrypted at rest (Fernet under the configured 32-byte key);
  the plaintext exists only in memory for the duration of one run.
- The DSN (which carries the database password) never reaches logs,
  exception messages, or the printed summary: ``redact_dsn`` masks the
  password and runner output is scrubbed before it is reported.
- Retention only deletes files matching the platform's own backup name
  pattern whose embedded timestamp parses; anything else is never touched.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cloud_platform.core.config import Settings
from cloud_platform.core.secrets import FernetSecretBox, MasterKey, SecretBoxError
from cloud_platform.observability.metrics import metrics

logger = logging.getLogger(__name__)

#: Backup files look like ``cloud-backup-20260824-120000.dump.enc``.
BACKUP_PREFIX = "cloud-backup-"
BACKUP_SUFFIX = ".dump.enc"
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

#: Files whose names do not parse are never deleted by retention.
_BACKUP_NAME = re.compile(
    rf"^{re.escape(BACKUP_PREFIX)}(\d{{8}}-\d{{6}}){re.escape(BACKUP_SUFFIX)}$"
)

#: How pg_dump is invoked (injectable for tests; async so the event loop
#: stays responsive while the dump streams).
PgDumpRunner = Callable[[Sequence[str]], Awaitable[subprocess.CompletedProcess[bytes]]]


class BackupError(Exception):
    """A backup run failed (dump, encryption, or I/O)."""


def build_pg_dump_command(dsn: str) -> list[str]:
    """The pg_dump command for a DSN (plain format, no ownership metadata)."""
    return [
        "pg_dump",
        "--format=plain",
        "--no-password",
        "--no-owner",
        "--no-privileges",
        dsn,
    ]


def redact_dsn(dsn: str) -> str:
    """The DSN with its password masked (safe for logs and errors)."""
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return "<unparseable dsn>"
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return dsn


def _scrub(text: str, dsn: str) -> str:
    """Remove the DSN and its password from external output before reporting."""
    if not text:
        return ""
    scrubbed = text.replace(dsn, redact_dsn(dsn))
    try:
        password = urlsplit(dsn).password
    except ValueError:
        password = None
    if password:
        scrubbed = scrubbed.replace(password, "***")
    return scrubbed.strip()


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Outcome of one encrypted backup run."""

    filename: str
    size_bytes: int
    duration_seconds: float
    deleted_files: tuple[str, ...]  # expired backups pruned this run


@dataclass(frozen=True, slots=True)
class BackupConfig:
    """Where to dump from, where to write, and with what key/retention."""

    dsn: str
    output_dir: Path
    retention_days: int
    key: MasterKey

    def __post_init__(self) -> None:
        if not self.dsn or not self.dsn.strip():
            raise ValueError("dsn must not be empty")
        if self.retention_days < 1:
            raise ValueError("retention_days must be >= 1")


class PostgresBackupJob:
    """One run: dump -> encrypt -> write -> prune expired backups."""

    def __init__(self, config: BackupConfig, *, runner: PgDumpRunner | None = None) -> None:
        self._config = config
        self._runner = runner or self._run_pg_dump
        self._box = FernetSecretBox(config.key)

    async def _run_pg_dump(self, command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
        # The argv contains the DSN; run in a thread so the loop stays
        # responsive, and never log the argv itself.
        return await asyncio.to_thread(
            subprocess.run, list(command), capture_output=True, check=False
        )

    async def run(self) -> BackupResult:
        config = self._config
        async with metrics.job("postgres_backup"):
            started = time.perf_counter()
            now = datetime.now(UTC)
            config.output_dir.mkdir(parents=True, exist_ok=True)

            command = build_pg_dump_command(config.dsn)
            try:
                proc = await self._runner(command)
            except Exception as exc:
                raise BackupError(f"pg_dump could not be executed: {type(exc).__name__}") from exc
            if proc.returncode != 0:
                stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
                raise BackupError(
                    f"pg_dump failed (exit {proc.returncode}): {_scrub(stderr, config.dsn)[:500]}"
                )

            dump = proc.stdout or b""
            if not dump.strip():
                raise BackupError("pg_dump produced an empty dump")

            filename = f"{BACKUP_PREFIX}{now.strftime(_TIMESTAMP_FORMAT)}{BACKUP_SUFFIX}"
            try:
                token = self._box.encrypt_bytes(dump)
            except SecretBoxError as exc:
                raise BackupError(f"encryption failed: {exc}") from exc
            ciphertext = token.encode("ascii")
            (config.output_dir / filename).write_bytes(ciphertext)

            deleted = self._prune(now)
            duration = time.perf_counter() - started
            logger.info(
                "backup complete: %s (%d bytes) in %.2fs; pruned %d expired",
                filename,
                len(ciphertext),
                duration,
                len(deleted),
            )
            return BackupResult(
                filename=filename,
                size_bytes=len(ciphertext),
                duration_seconds=duration,
                deleted_files=tuple(deleted),
            )

    def _prune(self, now: datetime) -> list[str]:
        """Delete own backups older than the retention window; nothing else."""
        cutoff = now - timedelta(days=self._config.retention_days)
        deleted: list[str] = []
        for entry in sorted(self._config.output_dir.iterdir()):
            match = _BACKUP_NAME.match(entry.name)
            if match is None or not entry.is_file():
                continue  # not ours, or unparseable: never touch
            try:
                stamped = datetime.strptime(match.group(1), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
            except ValueError:
                continue
            if stamped < cutoff:
                entry.unlink()
                deleted.append(entry.name)
        return deleted


# ---------------------------------------------------------------------------
# Restore (M11-008)
# ---------------------------------------------------------------------------


def parse_backup_filename(name: str) -> datetime:
    """The UTC timestamp embedded in a backup filename.

    Raises:
        ValueError: The name is not a platform backup filename.
    """
    match = _BACKUP_NAME.match(name)
    if match is None:
        raise ValueError(f"not a platform backup filename: {name!r}")
    return datetime.strptime(match.group(1), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def list_backups(output_dir: Path) -> list[Path]:
    """All platform backup files in the directory, newest first.

    Foreign files and unparseable names are never listed.
    """
    out: list[tuple[datetime, Path]] = []
    if not output_dir.is_dir():
        return []
    for entry in output_dir.iterdir():
        try:
            stamped = parse_backup_filename(entry.name)
        except ValueError:
            continue
        if entry.is_file():
            out.append((stamped, entry))
    out.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _stamped, path in out]


class RestoreError(Exception):
    """A restore run failed (missing file, bad key, I/O)."""


def build_pg_restore_command(dsn: str) -> list[str]:
    """The psql command that applies a plain-format dump to the DSN.

    The dump is plain SQL (``pg_dump --format=plain``), so ``psql`` is the
    restore vehicle; ``--single-transaction`` makes the restore all-or-nothing.
    """
    return ["psql", "--single-transaction", "--quiet", dsn]


class PostgresRestoreJob:
    """One restore: pick a backup -> decrypt -> stream through psql.

    Acceptance: **restore into a clean environment verified.** The job is the
    documented restore path (see docs/operations/RUNBOOK.md "Restore drill"):

    - The backup file must exist and carry a valid embedded timestamp;
      ``latest=True`` picks the newest platform backup.
    - The file is decrypted with the configured 32-byte key (a key that does
      not match the one used for the backup fails the Fernet MAC and raises
      ``RestoreError`` before anything reaches the database).
    - The decrypted SQL is streamed to psql against the target DSN; a
      non-zero psql exit (constraint failure, etc.) is a ``RestoreError``
      that carries the scrubbed output (no DSN, no password).
    - The DSN never reaches logs or exception messages (``redact_dsn`` /
      ``_scrub``, same discipline as the backup job).

    The job NEVER deletes or modifies the backup file: a failed restore must
    leave the source pristine for a retry.
    """

    def __init__(
        self,
        *,
        dsn: str,
        key: MasterKey,
        output_dir: Path,
        runner: Callable[[Sequence[str], bytes], Awaitable[subprocess.CompletedProcess[bytes]]]
        | None = None,
    ) -> None:
        if not dsn or not dsn.strip():
            raise ValueError("dsn must not be empty")
        self._dsn = dsn
        self._key = key
        self._output_dir = output_dir
        self._box = FernetSecretBox(key)
        self._runner = runner or self._run_psql

    async def _run_psql(
        self, command: Sequence[str], sql: bytes
    ) -> subprocess.CompletedProcess[bytes]:
        # The argv contains the DSN; run in a thread and pipe the SQL in via
        # stdin (it is already decrypted in memory for this one run).
        return await asyncio.to_thread(
            subprocess.run,
            list(command),
            input=sql,
            capture_output=True,
            check=False,
        )

    def select_backup_file(self, filename: str | None = None, *, latest: bool = True) -> Path:
        """Resolve which backup file to restore.

        ``filename`` names one file explicitly; otherwise (or with
        ``latest=True``) the newest platform backup is chosen.
        """
        backups = list_backups(self._output_dir)
        if filename is not None:
            for path in backups:
                if path.name == filename:
                    return path
            raise RestoreError(
                f"backup file {filename!r} not found in {self._output_dir} "
                f"(known: {[p.name for p in backups] or 'none'})"
            )
        if not latest and not backups:
            raise RestoreError(f"no backups found in {self._output_dir}")
        if not backups:
            raise RestoreError(f"no backups found in {self._output_dir}")
        return backups[0]

    async def run(self, filename: str | None = None, *, latest: bool = True) -> str:
        """Restore one backup; returns the restored filename.

        The whole sequence is fail-fast: file resolution, decryption and the
        psql run each raise ``RestoreError`` on failure; the backup file is
        never modified or deleted.
        """
        started = time.perf_counter()
        backup = self.select_backup_file(filename, latest=latest)
        try:
            ciphertext = backup.read_bytes().decode("ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise RestoreError(f"cannot read backup {backup.name}: {type(exc).__name__}") from exc
        try:
            sql = self._box.decrypt_bytes(ciphertext)
        except SecretBoxError as exc:
            # Wrong key (or a corrupted file): the Fernet MAC failed, so
            # nothing is applied - the restore is verifiably aborted.
            raise RestoreError(
                f"decryption failed for {backup.name} (wrong key or corrupted "
                f"file): {type(exc).__name__}"
            ) from exc
        if not sql.strip():
            raise RestoreError(f"backup {backup.name} decrypted to an empty dump")

        command = build_pg_restore_command(self._dsn)
        try:
            proc = await self._runner(command, sql)
        except Exception as exc:
            raise RestoreError(f"psql could not be executed: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
            raise RestoreError(
                f"psql failed (exit {proc.returncode}) restoring {backup.name}: "
                f"{_scrub(stderr, self._dsn)[:500]}"
            )
        logger.info(
            "restore complete: %s (%d bytes) in %.2fs",
            backup.name,
            len(sql),
            time.perf_counter() - started,
        )
        return backup.name


def build_backup_config(settings: Settings) -> BackupConfig:
    """Assemble the backup config from platform settings.

    The asyncpg DSN is translated to the plain postgres scheme pg_dump
    accepts; the encryption key comes from ``backup_encryption_key``
    (32-byte url-safe base64, env-provided — never hardcoded).
    """
    dsn = settings.database_url
    if dsn.startswith("postgresql+asyncpg://"):
        dsn = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
    if not settings.backup_encryption_key:
        raise BackupError(
            "backup_encryption_key is not configured; provide a 32-byte "
            "url-safe base64 key via the BACKUP_ENCRYPTION_KEY environment variable"
        )
    try:
        material = base64.urlsafe_b64decode(settings.backup_encryption_key.encode("ascii"))
        key = MasterKey(material=material)
    except (ValueError, UnicodeEncodeError) as exc:
        raise BackupError(
            "backup_encryption_key must be url-safe base64 of exactly 32 bytes"
        ) from exc
    return BackupConfig(
        dsn=dsn,
        output_dir=Path(settings.backup_output_dir),
        retention_days=settings.backup_retention_days,
        key=key,
    )
