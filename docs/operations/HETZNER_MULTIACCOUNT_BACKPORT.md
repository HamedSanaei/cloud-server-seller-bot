# Hetzner multi-account production backport

This release starts from production commit b6778310fd4095fc3d56d2ebcc3b149362671248
and keeps migration head 0053. No migration, dependency, wallet, billing, or
database model file changes. Existing AtlasPay, IRT prepayment, server deletion,
password reset and plan-family grouping remain on the production implementation.

Hetzner credentials are server-owned TOML entries under providers.hetzner.accounts.
The original token remains the explicit default account. New resources use active
accounts in priority order; existing resources retain their credential owner.
Only a definitive provider capacity refusal authorizes another POST on another
account. Uncertain outcomes recover read-only on the original attempted account.
Operation JSON receipts and existing owner columns serialize/fence those attempts.
The IRT checkout retains one original conversion quote, hold, and price snapshot
while its unaccepted provider create can fail over.

Dockerfile.hetzner-backport pins the exact existing production runtime digest and
replaces only /app/src. It introduces no dependency update and keeps the image's
entrypoint, database models and migrations from the running production release.

Deployment must first check source migration head and physical schema parity,
read both credential Projects, and refresh account-bound catalog observations.
Recreate API/worker/bot against the same validated image; stop the old exclusive
bot/worker consumers before replacement. Keep the old image and an owner-only
deploy.env backup for rollback. Never downgrade the database. Configuration stays
server-owned and never enters the source archive or image.

Verification includes synthetic monthly/hourly/legacy failover tests, production
IRT reservation tests, and the opt-in scratch PostgreSQL receipt tests. Full test
and typecheck results must be compared to b6778310: that base has pre-existing
failures and must not be described as a fully green baseline.

## Deployment evidence (2026-10-14)

- Host: Nethederland (95.211.62.26), application path /opt/cloud-server-seller.
- Running release: f88176993abc5eeb302f0d7e6c2b1325779a97ba.
- API, worker and bot use that same image/revision. API health/readiness passed;
  all three remained running with zero restarts and no ERROR/TelegramConflictError
  entries in the checked post-rollout logs.
- Active credential routes: default and peru; six eligible locations per account.
  NULL/default resource ownership still resolves to the original credential.
- Image preflight constructed both billing paths, account routing, order recovery,
  provisioning and management. Both Projects authenticated and returned EUR.
- Physical schema: 41 tables verified. Database and release head remain 0053.
- Dedicated Hetzner/unit checks: 282 passed. Production IRT/failover/repair checks
  after the last source change: 49 passed. Ruff lint and format checks passed.
- Isolated real PostgreSQL receipt/fencing tests: 71 passed. The disposable test
  PostgreSQL container and its SSH tunnel were removed after verification.
- Full suite comparison: backport 5406 passed / 52 failed / 179 skipped; production
  base 5178 passed / 53 failed / 108 skipped. No new failing test IDs. The 42 mypy
  errors and five provider-branching gate findings also exist in the base.
- Rollback keeps the original b6778310 image. Owner-only environment backup:
  /opt/cloud-server-seller/deploy.env.before-hetzner-20261014T183215Z.
- Local source is committed on fix/hetzner-multi-account-production. The image was
  built on the deployment host; this task did not publish it to GHCR or push the
  branch. Provider credentials remain exclusively in private configuration.

## Recharge hotfix (2026-10-15, Asia/Tehran)

- Current deployed release: 836cd0ffb5975348badd7f38f495e897083dcac9.
- Bot logs confirmed BUTTON_DATA_INVALID when a custom recharge amount opened
  the gateway picker. The Tetraminator callback unnecessarily included the
  AtlasPay nonce, exceeding Telegram's 64-byte limit for a six-digit amount.
- Recharge start now has a compact, reversible route alias with the same
  canonical HMAC. Existing literal callbacks still verify. Only AtlasPay carries
  its attempt nonce; neither the amount nor the signature is truncated.
- 158 targeted navigation, UI, recharge, FX and AtlasPay tests passed. Ruff lint
  and formatting passed. The production runtime rendered both gateways for
  100000, 275000 and int64-max amounts; maximum callback size was 63 bytes.
- API readiness/health passed; API, worker and bot run the same release with zero
  restarts. Checked startup logs contained no errors or Telegram polling conflicts.
- No schema, credentials or dependency changes; no real payment was initiated.
- Rollback image: f88176993abc5eeb302f0d7e6c2b1325779a97ba. Private environment
  backup: /opt/cloud-server-seller/deploy.env.before-recharge-20261014T232005Z.
- Source remains on fix/hetzner-multi-account-production; image is built locally
  on the host, not published to GHCR. This hotfix does not push the branch.
