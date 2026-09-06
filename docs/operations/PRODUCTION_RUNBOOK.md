# Production deployment runbook (M12-005)

Covers what the acceptance requires: **secrets, migrations, rollback and
health checks** - in the order a deploy actually runs them. The staging
deployment (M12-004) is the template; production differs only in the
secret source, the registry, and the fact that rollback is a real decision.

## 0. Before you start (every deploy)

1. The release commit is green in CI - **including the migration
   compatibility gate** (M12-003). An unsafe schema change is not a deploy
   problem, it is a build failure; do not work around it.
2. You know the current production image (its full SHA tag) - write it
   down; it is your rollback target.
3. A fresh encrypted backup exists (M11-007 job) or has just been taken.
4. If the release contains a migration, re-read the migration notes below
   even if the gate passed.

## 1. Secrets

**What production needs and where it comes from** (never from a file in the
repository; the compose file only references `${VAR}`):

| Variable | What | How it is protected |
| --- | --- | --- |
| `STAGING_DB_PASSWORD` (prod: the prod DSN password) | PostgreSQL | secret manager env injection; rotated on schedule |
| `STAGING_TELEGRAM_BOT_TOKEN` | Telegram bot token | secret manager; rotation = revoke + re-issue in BotFather |
| `STAGING_HETZNER_API_TOKEN` | provider API token | secret manager; **encrypted at rest in the DB** by the provider-credential key (M11-005/M09) |
| `STAGING_PROVIDER_CREDENTIAL_KEY` | 32-byte base64 key encrypting provider credentials at rest | secret manager; rotating it is the M10-008 workflow (re-encrypt, then swap) |
| `STAGING_BACKUP_ENCRYPTION_KEY` | 32-byte base64 key for backup files | secret manager; the key for an existing backup must stay available for its whole retention window |

Rules:

- Secrets are injected as environment variables by the deploy tooling;
  they are never written to disk, logs, or images. The redaction layer
  (M06-004/M08) masks anything that leaks, but the goal is that nothing
  leaks in the first place.
- Key material is 32 bytes, url-safe base64, generated with the platform's
  helper - see `deploy/staging/.env.example` for the command.
- A backup's encryption key is pinned to the backups made with it: keep
  the old key available until the last backup made with it expires
  (retention days). The restore drill (RUNBOOK.md → Restore drill, M11-008)
  fails loudly (Fernet MAC) if a key does not match, so a wrong key is
  never silent.
- Rotation: use the M10-008 rotation workflow when implemented; until
  then, rotation = add new key env var, deploy, run the re-encryption
  job, then remove the old key on the next deploy.

### 1a. Rotate a provider credential (M10-008) — no downtime

The live provider credential (Hetzner token / ArvanCloud key) is held at
runtime in a per-process `CredentialHolder`; adapters read it at REQUEST
time. Rotation is **verify-first, atomic, audited** — a failed rotation
changes nothing, so it can never take the live credential offline.

Procedure (operator):

1. Obtain the new credential from the provider (the provider-side overlap
   window keeps the old one valid until step 5).
2. Call the rotation endpoint / service
   (`CredentialRotationService.rotate(provider_key, new_credential_value,
   reason, actor)` — admin-gated, lands with the admin API, M14-003).
   The service:
   - verifies the candidate against the provider with a read-only call
     (`GET /datacenters` for Hetzner; `GET /regions/{r}/servers` for
     ArvanCloud). Any rejection aborts — the live credential keeps
     serving, zero impact.
   - atomically swaps the holder: in-flight requests finish on the old
     value, the next request uses the new one. No restart, no downtime.
   - writes an audited `credential.rotate` event carrying only key
     FINGERPRINTS (`key_hint`, sha256-truncated) — never the values.
3. Watch: provider-call metrics for `401/403` (an auth error after a
   successful rotate means the swap raced a provider-side revoke).
4. Only after the overlap window is confirmed healthy, revoke the old
   credential on the provider side.
5. The audit trail shows `previous_key_hint` / `new_key_hint`; the old
   value itself was never persisted or logged.

Scope note: the holder is per API process. The worker process picks up a
newly rotated credential at its next (re)start; during the overlap window
both credentials are valid at the provider, so in-flight worker work is
unaffected. The master-key (at-rest encryption) re-encryption path
(`EnvelopeService.rotate`) is a separate, offline operation.

## 2. Migrations

The deploy runs `migrate` (alembic upgrade head) **before** the api and
worker start - the compose `depends_on: condition:
service_completed_successfully` makes the first deploy and every later
deploy run the same sequence.

**Safe by construction (M12-003 gate):** the gate blocks, on the upgrade
path, anything that breaks old code running against the new schema -
column/table drops, renames, and defaultless NOT NULL. A green gate means
the upgrade is backward-compatible, which is exactly the property a
rolling deploy needs.

**Therefore the operating rules:**

1. Additive changes (new tables, nullable columns, columns with server
   defaults) are deploy-safe.
2. A destructive change (drop/rename) is a **two-phase rollout**:
   - Phase 1: ship code that no longer reads the column (or the new name),
     deploy everywhere.
   - Phase 2: in a later release, the migration drops it - with a
     `# compat-override: <reason>` naming the phase-1 release and the date.
     The override is printed by the gate and stays in the git history: it
     is an auditable decision, not an escape hatch.
3. Never edit an applied revision. Fix forward with a new revision.
4. `alembic downgrade` in production is a **manual, considered action**,
   not a button: only downgrade migrations that are purely additive and
   whose data you are willing to orphan. Financial tables (wallets,
   ledger, accrual periods) are append-only; their downgrades are
   deliberately not part of the deploy path.
5. Verify after the deploy: `alembic current` must show the head revision
   of the release commit (the post-deploy smoke test, M12-007, does this
   for you).

## 3. Rollback

Decision rule: **code first, schema only when safe.**

1. **Code rollback (the common case):** point `PLATFORM_IMAGE` back at
   the previous release's SHA tag and `docker compose up -d`. Because the
   migration gate guarantees upgrades are backward-compatible, the old
   code works against the new schema - no downgrade needed. This is
   repeatable and safe for financial data (the ledger is append-only;
   in-flight operations reconcile via the idempotency keys).
2. **Schema rollback (rare):** only for purely additive migrations,
   `alembic downgrade -1` by hand, after the code rollback, and only if
   the new schema would actually break the old code (which the gate
   should have prevented). Document the decision in the incident log.
3. **What rollback never does:** rewrites ledger/wallet rows, deletes
   accrual periods, or touches a provider resource. A provider operation
   that was already applied is reconciled, not undone (404-on-delete is
   success; the final charge is idempotent - see the operations module).
4. **If the rollback itself fails:** stop. Keep the current version
   running, open an incident, and fix forward on the next release.
   Financial integrity outranks availability (incident priority,
   RUNBOOK.md).

The immutable SHA tagging (M12-002) is what makes this honest: every tag
you have ever run still exists, un-mutated, in the registry.

## 4. Health checks

Post-deploy, in this order. **The M12-007 smoke test automates items
1-5 and the provider read (6)**:

```bash
uv run python scripts/post_deploy_smoke.py \
    --base-url http://127.0.0.1:8000 \
    --provider-key hetzner     # optional; uses HETZNER_API_TOKEN from the env
```

1. **Liveness:** `GET /health/live` → `{"status":"ok"}`.
2. **Readiness:** `GET /health/ready` → `{"status":"ok", ...}` (gains
   PostgreSQL/Redis checks when wired).
3. **Migrations:** `alembic current` == the release's head revision.
4. **Metrics:** `GET /metrics` serves; watch
   `cloud_platform_api_requests_total` (no new 5xx wave),
   `cloud_platform_job_runs_total` (jobs running), and the
   provisioning-failure counter (M11-006 alert feeds).
5. **Worker:** the arq log shows the registered job list and no crash
   loop; Redis `DBSIZE`/client count look normal.
6. **Provider (read-only, safe):** a token-scoped read call
   (e.g. list own resources) returns 200 - proves the credentials and
   network path are alive. **Never** a mutating call as a smoke test.
7. **Alerts:** confirm the M11-006 rules did not fire (spend, 429,
   provisioning failure, queue age, reconciliation drift).

**Kill switches** (RUNBOOK.md): if health is bad after rollback, disable
new provisioning / the provider account / the offer - a controlled stop
with clear status beats an ambiguous retry.

## Deploy strategies (M12-006)

The deploy driver encodes the consumer-safety rules as a validated plan
(`scripts/deploy.py`): migrations always run first and once; the stateless
api rolls behind health checks; the EXCLUSIVE consumers (worker = arq
jobs, bot = Telegram long-polling) are **fully drained - verified against
real `compose ps` output - before any new generation starts**, in both
strategies. A plan that would double-consume is refused before anything
executes.

```bash
# preview + validate (no changes)
uv run python scripts/deploy.py --image $PLATFORM_IMAGE --strategy rolling --dry-run
# rolling: health-gated api recreation, then drain->start per exclusive service
uv run python scripts/deploy.py --image $PLATFORM_IMAGE --strategy rolling
# blue-green: green sibling project takes traffic; blue drains first
uv run python scripts/deploy.py --image $PLATFORM_IMAGE --strategy blue-green
```

Blue-green detail: migrate runs ONCE against the shared DB; the green api
starts and must pass its health gate; only then are ALL blue worker/bot
generations stopped and verified gone, the green ones start, traffic cuts
over, and the blue api container is retired. Rollback = re-run the driver
with the previous image ref (migrations are forward-only compatible per
the M12-003 gate).

## Deploy checklist (copy per release)

- [ ] CI green incl. migration gate; release commit SHA noted
- [ ] previous production SHA noted (rollback target)
- [ ] fresh encrypted backup taken
- [ ] secrets for the release confirmed present (no new key needed / keys injected)
- [ ] `PLATFORM_IMAGE=<registry>/cloud-server-platform:<full-sha>` set
- [ ] `docker compose up -d` - migrate exits 0, api/worker up
- [ ] post-deploy smoke test green (health, alembic head, metrics, worker, provider read)
- [ ] alerts checked for 15 minutes
- [ ] incident log updated (or nothing to record)
