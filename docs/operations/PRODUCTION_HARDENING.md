# Production hardening

What makes this deployment safe to run for real customers: shared transient
state, the deployment topology and its honest limits, health/diagnostics,
graceful shutdown, the quality gates, and the steps a human still has to take.

Companion documents:

* [`../telegram/SESSION_STORAGE.md`](../telegram/SESSION_STORAGE.md) — Redis key
  layout, TTLs, atomic consumption, failure policy.
* [`../commerce/RENEWAL_LIFECYCLE.md`](../commerce/RENEWAL_LIFECYCLE.md) —
  commercial state, exactly-once wallet collection, grace and suspension.
* [`../leaseweb/VPS_API_COVERAGE.md`](../leaseweb/VPS_API_COVERAGE.md) — the
  provider contract and its coverage matrix.
* [`RUNBOOK.md`](RUNBOOK.md) / [`PRODUCTION_RUNBOOK.md`](PRODUCTION_RUNBOOK.md) —
  day-2 operations, backup and restore.

---

## 1. What "hardened" means here

| Concern | Guarantee | Where |
| --- | --- | --- |
| bot restart loses open buttons | references, prompts and confirmations survive | Redis + AOF (session doc §8) |
| two replicas both consume a confirmation | the claim is atomic in Redis; one winner | `SharedConfirmationStore` |
| Redis unavailable during a mutation | **fail closed**: refuse, never guess | `SessionStoreUnavailable` |
| Telegram redelivers a callback | the pending action is single-use (`take`) | `bot/sessions.py` |
| money charged twice | deterministic hold key per (service, period) | `RenewalChecker` |
| ambiguous provider outcome re-sent | recorded for attention, never re-POSTed | `OrderWorker`, `ServerManagementService` |
| wallet ledger mutated in place | append-only; only hold → capture writes | `modules/wallet` |
| provider secret in a log | redaction + builders with no secret parameter | `core/logging.py`, business-log events |
| deploying the wrong migrate revision | post-deploy smoke asserts the head | `scripts/post_deploy_smoke.py` |

---

## 2. Topology

```text
                    ┌──────────────┐
   Telegram ────────│  bot         │  replicas: 1, long polling (see §4)
                    └──────┬───────┘
                           │
   Internet ────────►┌─────▼──────┐      ┌──────────────┐
   (API_PORT)        │  api       │─────►│  postgres    │  (private network)
                    └─────┬──────┘      └──────────────┘
                          │                    ▲
                    ┌─────▼──────┐             │
                    │  worker    │─────────────┘
                    └─────┬──────┘      ┌──────────────┐
                          └─────────────►│  redis       │  (private network,
                                         └──────────────┘   no published port)
```

`deploy/production/docker-compose.yml`:

* `migrate` runs once to completion; `api`, `worker` and `bot` wait for it;
* `api`, `worker` and `bot` wait for healthy `postgres` **and** `redis`;
* only `api` publishes a port (`${API_PORT:-8000}`); `postgres` and `redis`
  publish none;
* `redis` runs `redis-server --appendonly yes` with a named volume and a
  `redis-cli ping` healthcheck;
* the platform image is referenced as `${PLATFORM_IMAGE}` — a deployed release
  is a full commit SHA, never `latest`.

Validate before deploying:

```bash
PLATFORM_IMAGE=registry/repo:<full-sha> POSTGRES_PASSWORD=… \
  docker compose -f deploy/production/docker-compose.yml config
```

Automated deployments are described in
[`PRODUCTION_DEPLOY.md`](PRODUCTION_DEPLOY.md): every green `main` commit is
built into an immutable GHCR image and deployed over SSH with
migrations-first ordering, health gates and image rollback.

---

## 3. Scaling rules (which service, and why)

| Service | Scale | Why |
| --- | --- | --- |
| `api` | freely | stateless; identity comes from tokens, rate limits are per-instance |
| `worker` | freely | financial concurrency is settled in PostgreSQL (row locks + unique hold keys) |
| `bot` | **1** | Telegram long polling is a single-consumer transport (§4) |

The worker is safe to replicate because Redis is never used as a financial lock.
Two workers may attempt the same renewal; the database decides the winner.

---

## 4. The polling constraint (be honest about it)

Sharing Redis makes *bot-managed state* safe across replicas. It does **not**
make the Telegram transport safe across replicas: two processes polling the same
bot token contend for updates and `getUpdates` conflicts.

```text
bot replicas = 1
```

This is enforced at the service definition (`deploy.replicas: 1`) and repeated
in the compose comments. A rolling deploy briefly overlaps the old and new
container; because the state is shared, the new container resolves the buttons
the old one rendered. That overlap is tolerable — permanent double polling is
not.

To run more than one bot process, move to a **webhook** first (the FastAPI app
already exists; that migration is a separate, deliberate change).

---

## 5. Health, readiness and diagnostics

* `/health/live` — process is up.
* `/health/ready` — dependencies considered ready for traffic.
* `scripts/post_deploy_smoke.py` — asserts live/ready/metrics plus the expected
  **migration head**, and performs read-only provider checks only.

Pre-flight before a release:

```bash
uv run python -m cloud_platform.cli leaseweb doctor
```

The doctor is read-only and never mutates a provider resource. It reports, among
others:

```text
[OK ] LEASEWEB_API_KEY configured — set (***************1234)
[OK ] Ordering API reachable
[OK ] Database query
[OK ] Redis reachable
[OK ] Telegram sessions — redis read/write ok
[OK ] Telegram sessions shared
```

Two of those lines exist specifically for this hardening work:

* **Redis reachable** — the shared store answers `PING`.
* **Telegram sessions** — a real write/read/delete round-trip through the
  selected backend, so a configured-but-broken store fails here rather than in
  front of a customer. `backend = "memory"` is refused outside development, so a
  misconfigured production deployment shows:

  ```text
  [FAIL] Telegram sessions shared — backend='memory' — a production
         deployment needs `redis` ([telegram.sessions] backend)
  ```

No secret is printed — the key is shown redacted (tail characters only).

---

## 6. Graceful shutdown

* **bot** — stop receiving updates, let in-flight handler work finish within a
  bounded timeout, then close the Redis client and the database engine.
  `close_container()` owns the Redis shutdown; it never raises to the caller.
* **worker** — never abandons a financial mutation in a way that causes a blind
  retry: a job that may already have transmitted a provider request records an
  ambiguous outcome and stops, rather than re-sending.
* **api** — closes its database engine and exits.

There is no unbounded wait anywhere; a shutdown that cannot finish cleanly is
logged and the process exits, because every durable guarantee lives in
PostgreSQL, not in a process.

---

## 7. Secrets

* Provider keys, the Telegram token and database credentials come from the
  environment / `configuration.toml` — never from source, never committed.
* Logging applies structured redaction for `Authorization`, `X-LSW-Auth`,
  tokens, passwords and cloud-init secrets.
* The business-log builders have no parameter for a secret, so an event
  physically cannot carry one.
* Console URLs, credential values and passwords are redacted in `repr`, are
  never persisted and never placed in a business event.
* `detect-secrets` (pre-commit) scans every commit against
  `.secrets.baseline`; the baseline currently covers only known false positives
  (example env files, test fixtures, the baseline itself).

---

## 8. Gates

Run all of these before considering a change done:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run pytest --cov=src --cov-fail-under=88

python scripts/check_migrations.py
python scripts/check_domain_provider_branching.py
python scripts/validate_tasks.py
python scripts/gen_leaseweb_coverage.py --check
```

Plus, when deployment files change:

```bash
docker compose config
docker compose -f deploy/production/docker-compose.yml config
```

and the secret scan (`detect-secrets` via pre-commit, or
`python -m detect_secrets scan --baseline .secrets.baseline`).

Notes on two of them:

* `check_migrations.py` analyses every revision; a migration must be
  non-destructive and must not override a revision.
* `gen_leaseweb_coverage.py --check` fails if
  `docs/leaseweb/VPS_API_COVERAGE.md` has drifted from the provider contract.

Repository policy: do not lower coverage, do not weaken a gate, and do not claim
a gate passed unless it was actually run.

---

## 9. Remaining manual steps before the first production order

Nothing below is code; it is deployment work a human must do.

1. **Provision Redis on the private network** with AOF (the compose file already
   does this) and confirm it is not reachable from the internet.
2. **Set `[telegram.sessions] backend = "redis"`** for the target environment
   and run the doctor; both `Redis reachable` and `Telegram sessions shared`
   must be `[OK]`.
3. **Run the migration** (`migrate` service) and confirm the post-deploy smoke
   reports the expected head.
4. **Keep `bot` at exactly one replica.** Do not raise it; migrate to a webhook
   first if more throughput is needed.
5. **Decide the commercial policy** explicitly: `[commerce.renewal]`
   thresholds, `grace_period_hours`, `auto_renew_default`, and
   `[commerce.suspension] stop_server_after_grace` (leave it `false` unless the
   product genuinely wants a server stopped for non-payment).
6. **Set `[features.server_management]`** for the deployment — start
   conservatively (`iso = false`), and confirm whether `billing = true` matches
   the launched product.
7. **Verify the wallet/settlement path end to end in staging** on a real,
   non-customer server: purchase → provision → renew-now with a funded wallet →
   confirm one ledger entry and one `service.renewal_succeeded` event.
8. **Confirm the operator channel** (`[telegram.logger]`) is a member and that
   the outbox is draining.
9. **Backups**: PostgreSQL backup/restore is runbook-owned. Telegram transient
   state is intentionally not backed up — losing it can only make an operation
   unavailable, never authorise one.
