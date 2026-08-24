# Operations runbook starter

## Golden signals
- API error rate/latency
- Telegram update processing delay
- worker queue depth/oldest job age
- provider API errors/429s and remaining rate limit
- provisioning success time
- reconciliation mismatch count
- wallet/ledger posting failures
- payment callback failures
- servers in `manual_review`
- provider spend vs customer billed amount

## Immediate kill switches to implement before launch
- disable all new provisioning
- disable a provider account
- disable a location/plan/offer
- freeze one user
- freeze payments
- pause destructive automation while preserving observation/reconciliation

## Incident priority
Financial integrity and accidental resource creation/deletion outrank feature availability. Prefer a controlled stop with clear status over ambiguous retries.

## Restore drill (M11-008)

The verified restore path. Backups are encrypted pg_dump files
(`cloud-backup-YYYYMMDD-HHMMSS.dump.enc`) written by
`python -m cloud_platform.backup` (M11-007) under `BACKUP_OUTPUT_DIR`,
encrypted with the 32-byte key in `BACKUP_ENCRYPTION_KEY`.

**Drill: restore into a clean environment** (the acceptance of M11-008):

1. Provision a clean PostgreSQL instance (fresh database, e.g.
   `postgresql://cloud:<pw>@clean-db:5432/clean`) and export its DSN.
2. Run the restore against it (the newest backup, or a named one):

   ```bash
   python -m cloud_platform.backup --restore --target-dsn "postgresql://cloud:<pw>@clean-db:5432/clean"
   python -m cloud_platform.backup --restore cloud-backup-20260820-093000.dump.enc --target-dsn "postgresql://..."
   ```

   The job selects the backup file, decrypts it with the configured key,
   and streams the SQL through `psql --single-transaction` (all-or-nothing).
   It prints `restore ok: <filename>` on success and
   `restore failed: <reason>` (scrubbed: no DSN, no password) otherwise.
3. Verify the clean environment: table count and a spot-check query
   (e.g. `select count(*) from wallets;`) match the production snapshot;
   the restore's single transaction means a failed restore leaves the
   clean DB untouched.

**Safety properties** (all covered by `tests/unit/test_restore_drill.py`):
- A wrong `BACKUP_ENCRYPTION_KEY` (or a corrupted file) fails the Fernet
  MAC and aborts BEFORE anything reaches the database.
- Only files matching the platform's own name pattern are ever selected;
  foreign files in the backup directory are invisible to the job.
- The backup file is never modified or deleted by a restore (a failed
  restore can be retried from the same file).
- psql output in error messages is scrubbed (DSN and password masked).

**Rollback of a bad restore:** the restore is one transaction into the
clean environment; dropping and re-creating the database (or restoring
from the previous backup file) is the rollback. Production is never the
restore target in the drill.

**Drill cadence:** run at least monthly, after any change to
`BACKUP_OUTPUT_DIR`, the encryption key, or the dump/restore commands;
record the result (date, backup file, verification queries) in the
incident log.
