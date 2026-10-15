# Deployment (GHCR image + SSH)

How a commit reaches the server — the fast `staging` development lane and the
manual `main` release lane — plus the one-time server bootstrap an operator
performs first. Companion documents:
[`PRODUCTION_HARDENING.md`](PRODUCTION_HARDENING.md) (topology and guarantees),
[`RUNBOOK.md`](RUNBOOK.md) (day-2 operations), [`PRODUCTION_RUNBOOK.md`](PRODUCTION_RUNBOOK.md).

## 1. Pipeline overview

```text
DEVELOPMENT LANE (active): push to `staging`
  -> deploy-staging workflow: fast static/import checks, targeted financial and
     deployment regressions, historical/fresh PostgreSQL migration contracts
  -> cached Docker build of the EXACT pushed SHA -> GHCR
  -> SSH deploy with scripts/deploy-production.sh DEPLOY_PROFILE=staging
  -> migrations -> api/worker/single-bot -> health + storefront readiness
  -> atomic compose promotion; the SAME Telegram bot is replaced in place

RELEASE LANE (manual while `staging` is the active lane): push to `main`
  -> `ci` workflow ONLY (ruff, mypy, secrets, pytest+coverage, migration,
     provider-branching, task, migration-smoke, leaseweb-contract gates)
  -> NO automatic deployment
  -> an operator dispatches `deploy-production` with a full 40-char SHA that
     must be an ancestor of `main`
       1. resolve that SHA (workflow input `sha`)
       2. check out that exact SHA
       3. build the Dockerfile, push ghcr.io/<org>/<repo>:<full-sha>
          (an existing SHA tag is never rebuilt or overwritten)
       4. verify the pushed image (entrypoint smoke + production compose config)
       5. SSH to the server, log in to GHCR with the short-lived GITHUB_TOKEN
       6. run scripts/deploy-production.sh with that exact image
       7. report; fail the workflow when health checks fail (even after rollback)
```

`main` deliberately does NOT deploy automatically: `staging` currently owns the
server and the single Telegram bot token, and a green `main` has no release
meaning during this phase. Automatic release delivery may only be restored
together with a server/bot of its own (a second token or a second environment);
until then both lanes would fight over one single-consumer token. Pull-request
CI runs never deploy.

Both lanes disable cancellation of active runs and use the same deploy-job
concurrency group, `telegram-shared-host-deploy`. Builds for one SHA are also
serialized. The deploy engine additionally holds a server-side `flock` through
failure recovery; direct operator invocations cannot bypass that exclusion.
Once mutation begins, the engine ignores HUP/INT/TERM rather than interrupting
a migration or service replacement. New pushes may replace pending runs, not
interrupt an active deployment.

### The staging lane in one screen

* **Trigger**: every push to `staging` (plus `workflow_dispatch`, which
  redeploys the current `staging` HEAD; a feature branch is never deployable
  through this lane).
* **Checks**: `uv sync --locked --all-groups`, the canonical staging
  static/import gate, targeted financial/worker/deployment regressions, the
  historical migration bridge, fresh physical-schema parity and live
  PostgreSQL contracts. A PostgreSQL 17 service and matching backup/restore
  clients are provided. Build/deploy depends on this job succeeding.
  The full release suite, coverage, mypy and pre-commit remain release gates;
  the staging pipeline does not substitute for them.
* **Image**: only `ghcr.io/<org>/<repo>:<pushed full SHA>`, never rebuilt or
  overwritten when it exists. Registry errors fail closed rather than being
  interpreted as a missing tag. No mutable convenience tag is deployed.
* **Target**: the SAME server, the SAME compose project
  (`cloud-platform-production`), the SAME server-owned
  `deploy.env`/`configuration.toml` and the SAME Telegram bot as production,
  because the existing GitHub `production` environment is where `PROD_SSH_KEY`,
  `PROD_KNOWN_HOSTS` and the `PROD_*` variables live (no duplicated secrets).
  That environment has no protection rules today, so the staging lane adds no
  approval step; if required reviewers are ever enabled there, move those
  values to a shared environment (or dedicated staging credentials) BEFORE,
  otherwise staging deploys inherit the approval.
* **Deploy engine**: the same `scripts/deploy-production.sh`.
  `DEPLOY_PROFILE=staging` skips only the one-shot provider catalog refresh,
  because the worker's scheduled coordinator owns provider facts.
  Configuration, migration/head/physical-schema, catalog normalization,
  single-poller, service health, financial reconciliation and storefront gates
  remain enabled.
* **Database**: the shared database, forward only (`alembic upgrade head`);
  never reset, recreated, restamped to hide a conflict, or downgraded.
* **Bot safety**: old application writers are stopped before migrations.
  Compose uses the explicit project `cloud-platform-production` and
  `bot.replicas: 1`. Promotion requires exactly one running, healthy bot and
  a fresh heartbeat written by its existing successful `getUpdates` poll;
  the health check never starts another poller.
* **Failure safety**: image rollback is conditional, not universally safe.
  An intended or observed schema advance leaves writers stopped for operator
  recovery; see §6. Active deployments are not cancelled by newer pushes.
* **Not** used by this lane: `deploy/staging/docker-compose.yml` (M12-004,
  `scripts/deploy.py`) describes a separate isolated stack with its own
  database, secrets and bot token; the staging lane replaces the running stack
  instead. Do not start a second database, Redis, deploy path, or bot.

## 2. Server layout and ownership

```text
RELEASE-OWNED (promoted automatically on every successful deploy):
    Docker image (ghcr.io/<org>/<repo>:<full-sha>)
    /opt/cloud-server-seller/docker-compose.yml   (canonical release contract)

RELEASE/TOPOLOGY INVARIANT (non-secret, enforced from release-owned compose):
    production Telegram transient-state backend = redis
    (TELEGRAM_SESSIONS_BACKEND=redis in the shared compose block; wins over
    a stale server TOML by documented precedence — no manual TOML edit needed)

SERVER-OWNED (never delivered from Git, never overwritten by a deploy):
    /opt/cloud-server-seller/deploy.env           (infrastructure values only, mode 600)
    /etc/cloud-server-seller/configuration.toml   (bot token, callback key,
      provider/payment credentials, database URL, encryption keys, mode 640 or 600)
    persistent Docker volumes/data, host credentials
```

Every deploy ships the exact `deploy/production/docker-compose.yml` from the
exact tested commit as a per-SHA candidate
(`.docker-compose.<FULL_SHA>.candidate.yml`), verifies its SHA-256, runs the
whole release against it, and atomically promotes it to the canonical path
only after all health/stability/revision/financial gates pass. Promotion is the
last fallible step. A failure leaves the previous canonical compose untouched;
it is used for automatic image rollback only when the database remains
compatible under §6. A manual older-SHA dispatch is not approval to undo a
financial/schema transition.

* `deploy.env` holds ONLY `PLATFORM_IMAGE`, `POSTGRES_PASSWORD`, `API_PORT`
  and optionally `CONFIGURATION_PATH`. Automated deploys rewrite a single
  line there (`PLATFORM_IMAGE`); the password is never rewritten.
* `configuration.toml` is mounted read-only into every container. It is
  never committed, never uploaded from the repository, and never overwritten
  by a deployment — the deploy script only checks that it EXISTS.
* No production application secret lives in GitHub (repository, workflow, or
  logs). The only credentials GitHub holds are the SSH access below and the
  ephemeral `GITHUB_TOKEN` used for one GHCR pull per deploy.

## 3. One-time server bootstrap (Ubuntu 22.04/24.04)

Run as root (or with sudo). The deploy user never runs the app as root; it
only needs Docker access.

```bash
# 1. user + Docker Engine + Compose plugin
adduser --disabled-password --gecos '' deploy
usermod -aG docker deploy
apt-get update && apt-get install -y ca-certificates curl gnupg python3 openssh-server
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# 2. directories (deploy owns its dir; the TOML is root-owned, deploy-readable)
install -d -m 0750 -o deploy -g docker /opt/cloud-server-seller
install -d -m 0750 -o root -g docker /etc/cloud-server-seller

# 3. application configuration (the ONE canonical template lives at the
#    repository root; copy it OUT of a checkout, then edit it)
install -m 0640 -o root -g docker \
  configuration.example.toml \
  /etc/cloud-server-seller/configuration.toml
"$EDITOR" /etc/cloud-server-seller/configuration.toml   # fill in every secret
```

**Configuration contract (one template, one real file).**

| File | Owner | Purpose |
| --- | --- | --- |
| `configuration.example.toml` (repository root) | repository | the SINGLE canonical template: every supported key, safe fake values, production form documented in a comment next to each environment-dependent key |
| `configuration.toml` | operator, local only | the real values; git-ignored, never committed, never printed |
| `/etc/cloud-server-seller/configuration.toml` | operator, server | the real values the containers mount read-only |

The template is the contract, and a check enforces it: a new operator-facing
setting must be added to `configuration.example.toml` in the same change that
introduces it (see `tests/unit/test_config_toml.py`). There is deliberately no
second, production-flavoured example — the deleted `deploy/production/` copy
had drifted and silently lost/duplicated keys.

To upgrade an existing server: diff the template against your local real
`configuration.toml`, merge the missing keys, validate, then install it. A
deployment only ever mounts that file; it never writes or overwrites it.

Validate before installing — the read-only drift check prints KEY PATHS only,
never a value:

```bash
python -m cloud_platform.cli config doctor
```

Exit 0 = every supported key accounted for, no unknown keys, no unreplaced
`CHANGE_ME` placeholder in a production file; exit 1 = the file is unreadable or
invalid, a required key is missing, or a production placeholder survives.

```bash
# 4. deployment env only (the release compose arrives automatically;
#    do NOT copy configuration.toml here and do NOT copy docker-compose.yml —
#    the first successful deploy promotes its own canonical compose file)
cp deploy/production/.env.example /opt/cloud-server-seller/deploy.env
chmod 600 /opt/cloud-server-seller/deploy.env
"$EDITOR" /opt/cloud-server-seller/deploy.env
#   PLATFORM_IMAGE=<first image, filled by the first automated deploy;
#     for the very first deploy any valid ghcr.io SHAtag works, the workflow
#     overwrites it immediately>
#   POSTGRES_PASSWORD=<strong random, must match [database] url in the TOML>
#     generate: openssl rand -base64 32
#   API_PORT=8000

# 5. SSH access for GitHub Actions (append the operator-generated key)
install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh
cat prod_deploy_key.pub >> /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

# 6. firewall: SSH in, API port as desired, nothing else published
ufw allow OpenSSH
ufw allow 8000/tcp   # only if the API should be reachable directly
ufw --force enable

# 7. GHCR access needs no stored credential: every deploy logs in with the
#    short-lived GITHUB_TOKEN over the SSH session (never printed or saved).
```

First deployment: push to `staging` (or dispatch `deploy-production` with a
specific `main` SHA). The workflow pulls the image, runs migrations, starts
api + worker + one bot, and health-checks before declaring success.

## 4. GitHub configuration the operator must set manually

Repository settings:

* Enable **Dependency graph** and **Dependabot alerts** under Advanced Security.
  Repository administrators can enable both with
  `PUT /repos/{owner}/{repo}/vulnerability-alerts`. A disabled graph makes the
  dependency-review API return HTTP 403; fix the repository setting rather
  than skipping or weakening the dependency review check.
* Protect `main`: require a pull request, resolved conversations and the
  up-to-date **Required CI checks** status from the GitHub Actions application.
  Enforce these rules for administrators and disallow force pushes and branch
  deletion. Do not merge or dispatch a release before owner approval.

Environment **`production`** (repository → Settings → Environments):

| Type | Name | Value |
| --- | --- | --- |
| Secret | `PROD_SSH_KEY` | private OpenSSH key matching the server's `authorized_keys` |
| Secret | `PROD_KNOWN_HOSTS` | operator-verified `known_hosts` entries; authenticate fingerprints out-of-band before accepting `ssh-keyscan` output, and never disable host verification |
| Variable | `PROD_HOST` | production hostname or IP |
| Variable | `PROD_PORT` | SSH port (default `22` when unset) |
| Variable | `PROD_USER` | `deploy` |
| Variable | `PROD_DEPLOY_PATH` | `/opt/cloud-server-seller` |

Both lanes read this ONE environment (no duplicated infrastructure secrets):
`deploy-staging.yml` declares `environment: production` for exactly this
reason, and its deploy job joins the shared-host concurrency group so the two
lanes never overlap. Any change made here (rotation, renaming a variable)
applies to both lanes at once — verify both workflows afterwards.

Package visibility: the GHCR image may stay private. Each workflow supplies the
short-lived `GITHUB_TOKEN` on stdin within one SSH session. That session creates
a private temporary `DOCKER_CONFIG`, uses it for the pull/deploy, and removes it
on exit; the new login is not written to the server user's default Docker
credential file. Earlier pipelines may have left credentials there: inspect
their presence without printing values and rotate/remove them only with
operator authorization. Application secrets remain server-owned.

## 5. Deployment sequence (what the script does)

1. Acquire the shared-host lock; validate tools, bounded health settings,
   server-owned files and the candidate compose's SHA-256. Reject symlinks,
   invalid profiles and non-SHA image references before service mutation.
2. Pull the exact image; require its OCI revision to match its full SHA tag.
   Validate the mounted server configuration without printing its values.
3. Record the previous image, change only `PLATFORM_IMAGE` in `deploy.env`,
   and establish PostgreSQL/Redis health. Read the database and image heads;
   a database with multiple revisions is not deployable.
4. Stop API, worker and the single bot before `alembic upgrade head`.
   Alembic also holds a PostgreSQL transaction-scoped migration lock.
5. Require the database head to equal the image head and verify the physical
   schema, including the uniqueness/partial-index contracts used by upserts.
   A version stamp alone is not schema evidence.
6. Refresh provider facts (release profile only), then normalize catalog
   selling currency. These bounded idempotent operations report failures;
   the later read-only storefront gate remains authoritative during outages.
7. Start API, worker and one bot. Require API readiness, PostgreSQL/Redis
   health, ARQ worker health, a fresh existing-poller bot heartbeat, and stable
   worker/bot container identities and restart counts.
8. Recheck the migration head and read-only storefront readiness. Run
   `scripts/post_deploy_smoke.py`: database/worker checks and wallet/payment
   reconciliation share a read-only repeatable-read transaction. Smoke tests
   do not create invoices, credit wallets or provision provider resources.
9. Print service status, recheck candidate integrity and atomically promote
   the compose file. Images and persistent volumes are not automatically
   pruned; keep the previous image available for compatible recovery.

## 6. Migration and rollback behavior

### Ambiguous historical revision `0050`

The released histories used `0050` for different changes. The canonical chain
retains staging's `0050` through `0053`; additive bridge `0054` inspects the
physical schema and preserves the main identity/payment/prepay changes too.
Do not rename deployed revision stamps or use `alembic stamp` as a repair.
Fresh databases, `0049`, both physical variants of `0050`, and staging `0053`
are exercised by `tests/live/test_migration_bridge.py`, including rows,
indexes, constraints and physical parity at one head.

The bridge projects compatible payment evidence between legacy columns and
JSON details. Conflicting evidence or duplicated gateway/idempotency facts
abort the transaction rather than guessing, deduplicating or deleting money
history. Resolve such conflicts with an approved, audited data decision, then
retry the forward migration.

### Recovery rules

* Require a recent encrypted off-host backup and a successful clean-target
  restore drill before approving a schema/financial-epoch advance. Backups are
  not created or restored automatically by deployment.
* `alembic downgrade` is never part of recovery. Physical parity does not prove
  that an old image understands a new currency or financial epoch.
* After a migration is attempted, if either the target image head or observed
  database head differs from the pre-deploy revision, candidate writers are
  stopped and **old-image restart is refused**, even when the migration fails
  before stamping. Recover with a reviewed forward fix or a separately
  verified operator recovery procedure.
* When no schema transition was attempted, the prior image must pass physical
  parity before automatic restoration with the previous canonical compose.
  Failure recovery still leaves the workflow red.
* A first deployment has no rollback image; failed candidate writers are
  stopped. Never report an unavailable rollback target as success.
* Do not manually run `compose up` on an old image after a forward migration.
  Schema/data restoration is a separate destructive operation requiring
  explicit approval, an isolated restore verification and writer exclusion.

## 7. Logs, inspection, backup

```bash
cd /opt/cloud-server-seller
docker compose --env-file deploy.env -f docker-compose.yml ps
docker compose --env-file deploy.env -f docker-compose.yml logs --tail=200 api worker bot
docker compose --env-file deploy.env -f docker-compose.yml exec -T api alembic current
curl -fsS http://127.0.0.1:8000/health/ready
# one encrypted backup run (manual/scheduled, never part of deploy):
docker compose --env-file deploy.env -f docker-compose.yml --profile backup run --rm backup
```

Deployments never delete volumes (`docker compose down -v` is forbidden in
automation); the `backup` profile stays manual/scheduled.

## 8. Secrets discipline (quick reference)

GitHub Actions logs show: commit SHA, image tag, target path, migration
result, service status, health result, rollback status. They MUST never show:
the SSH private key, `POSTGRES_PASSWORD`, `configuration.toml` content, the
Leaseweb key, the Telegram token, the callback signing key, or payment
secrets. Configuration validation suppresses input values. The workflow sends
the GHCR token on stdin, unsets it and removes its private temporary Docker
credential directory on exit. Never bypass SSH host-key verification to deploy.
