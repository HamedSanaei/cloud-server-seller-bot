# Production deployment (GHCR image + SSH)

How every green `main` commit reaches the production server, and the one-time
server bootstrap an operator performs first. Companion documents:
[`PRODUCTION_HARDENING.md`](PRODUCTION_HARDENING.md) (topology and guarantees),
[`RUNBOOK.md`](RUNBOOK.md) (day-2 operations), [`PRODUCTION_RUNBOOK.md`](PRODUCTION_RUNBOOK.md).

## 1. Pipeline overview

```text
push to main
  -> `ci` workflow (ruff, mypy, secrets, pytest+coverage, migration,
     provider-branching, task, migration-smoke, leaseweb-contract gates)
  -> `deploy-production` workflow, ONLY when that CI run concluded `success`
       1. resolve the exact CI-tested SHA (github.event.workflow_run.head_sha)
       2. check out that exact SHA
       3. build the Dockerfile, push ghcr.io/<org>/<repo>:<full-sha>
          (an existing SHA tag is never rebuilt or overwritten)
       4. verify the pushed image (entrypoint smoke + production compose config)
       5. SSH to production, log in to GHCR with the short-lived GITHUB_TOKEN
       6. run scripts/deploy-production.sh with that exact image
       7. report; fail the workflow when health checks fail (even after rollback)
```

Failed CI deploys NOTHING: the deploy workflow's build job only starts for
`conclusion == success` on a `push` to `main`. Pull-request CI runs never
deploy. Deployments are serialized (`concurrency: production-deploy`,
`cancel-in-progress: false`): a newer green commit waits for the running
deploy to finish, then deploys afterwards — a deploy is never cancelled
halfway.

Manual redeploys (`workflow_dispatch`, input `sha`) follow the same
build → verify → deploy → health-check path with an explicit immutable SHA
(which must be an ancestor of `main`); there is no second, less-safe path.

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
only after all health/stability/revision gates pass. A failed release leaves
the previous canonical compose untouched and rolls back with it. Manually
copying compose to the server is NOT required for normal deploys (and a
manual redeploy of an older SHA ships THAT SHA's compose, never current
main's).

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

# 3. application configuration (copy the example OUT of a checkout, edit it)
install -m 0640 -o root -g docker \
  deploy/production/configuration.example.toml \
  /etc/cloud-server-seller/configuration.toml
"$EDITOR" /etc/cloud-server-seller/configuration.toml   # fill in every secret

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

First deployment: push to `main` (or dispatch the workflow with the tested
SHA). The workflow pulls the image, runs migrations, starts
api + worker + one bot, and health-checks before declaring success.

## 4. GitHub configuration the operator must set manually

Environment **`production`** (repository → Settings → Environments):

| Type | Name | Value |
| --- | --- | --- |
| Secret | `PROD_SSH_KEY` | private OpenSSH key matching the server's `authorized_keys` |
| Secret | `PROD_KNOWN_HOSTS` | `ssh-keyscan -p <port> <host>` output (never disable host verification) |
| Variable | `PROD_HOST` | production hostname or IP |
| Variable | `PROD_PORT` | SSH port (default `22` when unset) |
| Variable | `PROD_USER` | `deploy` |
| Variable | `PROD_DEPLOY_PATH` | `/opt/cloud-server-seller` |

Package visibility: the GHCR image may stay private — the workflow's
`GITHUB_TOKEN` pulls it during build/verify, and the same token logs the
server in per deploy. No personal access token is required. No other
repository secrets are needed: application secrets live only in the server
`configuration.toml`.

## 5. Deployment sequence (what the script does)

1. Validate tools (`docker`, compose plugin, `python3`, `sha256sum`) and
   required files; refuse to create or modify `configuration.toml` (fail
   if missing).
2. Verify the release candidate compose (`EXPECTED_COMPOSE_SHA256` over the
   transferred file, regular file, no symlink, parses with the NEW image).
   A mismatch fails before any mutation: no image switch, no migration, no
   restarts, no rollback.
3. Validate `PLATFORM_IMAGE_NEW` is `ghcr.io/<org>/<repo>:<40-hex-sha>`.
4. Record the current `PLATFORM_IMAGE` as the rollback image.
5. Rewrite ONLY the `PLATFORM_IMAGE` line in `deploy.env` (idempotent).
6. Pull the image (candidate contract); on pull failure restore the
   previous reference and stop (running containers untouched).
7. Start/verify `postgres` + `redis` (bounded health waits, candidate contract).
8. Run migrations (`alembic upgrade head` in the one-shot `migrate`
   service); on failure restore the previous reference and stop.
9. Start/update `api`, `worker`, and exactly one `bot` (candidate contract).
10. Health gates (all bounded, no infinite waits):
    * `GET /health/ready` reports `{"status": "ok"}` (strongest readiness gate);
    * `postgres` + `redis` containers healthy; `worker` running;
    * exactly one `bot` container running (long-polling single consumer);
    * `worker` + `bot` startup stability: same containers still running with
      unchanged restart counts after a short bounded wait (a crash loop must
      fail the deploy even if a container looks running for a moment);
    * `alembic current` inside `api` reports the expected head
      (`EXPECTED_HEAD`, resolved from the deployed commit in CI).
11. Atomically promote the candidate compose to the canonical path
    (same-filesystem rename, mode 0644) and log its SHA-256.
12. Print service status; on success prune older local images (current +
    rollback images are always kept).

## 6. Migration and rollback behavior

* Migrations run BEFORE the new code is declared healthy and are
  forward-compatible (enforced by the migration compatibility gate in CI),
  so rolling the application image back is always safe.
* `alembic downgrade` is NEVER run automatically.
* When health checks fail AFTER the switch, the script restores the previous
  `PLATFORM_IMAGE`, restarts the previous api/worker/bot **with the previous
  canonical compose (never the failed candidate)**, leaves the
  database at the new (forward) revision, and exits 1: **a successful
  rollback is still a failed deployment** (the workflow stays red).
* With no previous image (first deploy), a failure reports NO ROLLBACK
  TARGET loudly instead of pretending.
* Manual rollback (same guarantees, same health gates): set `PLATFORM_IMAGE`
  in `deploy.env` back to the previous SHA tag and re-run the script (or the
  workflow) — or `docker compose --env-file deploy.env up -d api worker bot`
  followed by the health checks in §5 step 9.

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
secrets. The deploy script handles none of these values; the workflow passes
the GHCR token on stdin only.
