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
