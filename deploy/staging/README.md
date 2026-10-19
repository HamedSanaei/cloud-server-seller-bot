# Staging — automatic fast release to the original production runtime

The operator explicitly selected **one shared runtime**, not an independent test
stack. A successful push to `staging` runs targeted staging checks, publishes the
exact commit image in GHCR and updates the original API, worker and Telegram bot.
It uses the **existing** production database, Redis, deployment directory,
server-owned `configuration.toml`, bot token and Compose project
`cloud-platform-production`. Never create a second bot poller or financial ledger.

`DEPLOY_PROFILE=staging` skips only the one-shot provider catalog refresh. The
worker's scheduled coordinator still refreshes the catalog; migration, financial,
health, reconciliation and storefront-readiness gates are unchanged. The actual
application environment remains `production`, including Console contract headers.

## Fast validation contract

`staging-checks.yml` runs two parallel jobs: Ruff/compile/import smoke plus targeted
financial/deployment regressions, and real PostgreSQL migration/locking/catalog
CAS/wallet-ledger-outbox contracts. Secret scanning and actionlint remain enabled.
The cached immutable image build follows both successful jobs. No full coverage
suite, mypy, historical migration bridge or redundant image build runs in this
lane; those stay in unchanged `ci.yml` for main/release. Fresh migration and
physical schema parity still run before deployment, as do all shared-host gates.

Locally run `uv run python scripts/verify_ci.py --staging --test <affected-test>`.
This is targeted evidence, not a claim of full release validation. Do not use the
shared server for synthetic tests or bypass a failed storefront price gate.

## GitHub configuration

The `staging` GitHub Environment is a release-credential/branch boundary, not a
separate application environment. Restrict its deployment branch to `staging`.
Configure these entries for the SAME existing production target:

- GitHub secret entries — `DEPLOY_HOST`, `DEPLOY_PORT`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`,
  `DEPLOY_KNOWN_HOSTS` (host key verified out of band).
- Variables: `DEPLOY_PATH`, `CONFIGURATION_PATH` (existing live private TOML).
- Grant the repository's scoped `GITHUB_TOKEN` access to the GHCR package.

The operator elected to install these secrets personally. Do not upload their
existing private SSH key or silently bypass an empty Environment. A normal main
push runs CI only. The manual main lane retains explicit dispatch confirmation
and protected `production` Environment approval; both lanes address the same data.

## Release and recovery

Use `deploy/production/docker-compose.yml` for both lanes. Both deploy jobs share
one concurrency group and the target-directory lock. The web/native engines also
hold `/var/lock/servercloud-platform-release.lock` using the same deployment OS
user, preventing cross-repository migration/restart overlap. Build only in CI.

The engine verifies image SHA/revision/contract, private configuration and existing
project ownership before draining writers. It encrypts a backup, migrates, checks
physical schema parity, starts the API/worker and exactly one bot, checks health
and stable restart counts, reconciles finance, then verifies storefront readiness.
After a schema/financial epoch transition, rollback never starts old writers or
downgrades the database; an approved forward fix or verified restore is required.

Read-only release inspection (substitute the existing live paths):

```bash
uv run python scripts/deploy.py --environment staging \
  --deploy-path /opt/cloud-server-seller \
  --configuration-path /etc/cloud-server-seller/configuration.toml \
  --image "ghcr.io/OWNER/REPOSITORY:FULL_40_CHARACTER_COMMIT_SHA" --dry-run
```

Do not use the shared live wallet or a customer's VPS for synthetic finance tests.
Network `/website` linking requires an actual authorized Telegram user interaction.
Live test-VPS mutations retain their separate identity/dependency/recovery and
operation-specific approval requirements.

The final-image application worker and a reconciliation queue job were exercised
against a disposable local DB/Redis namespace. Its lightweight PID-bound ARQ
health probe succeeded; this does not establish health of the still-old remote
worker or prove network delivery by the original Telegram bot.

Backup/restore: [operations runbook](../../docs/operations/RUNBOOK.md). Never run
`docker compose down -v` on an environment holding retained state.
