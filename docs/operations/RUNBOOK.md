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

## Selling from Hetzner: local bring-up (M08)

First milestone of the selling bot: browse the synced Hetzner catalog
(locations -> plans) from Telegram with signed M08-001 callbacks.

**Fast path** — one command does steps 1-3 (stack, schema, catalog sync):

```bash
uv run python scripts/setup_dev.py        # skips sync if HETZNER_API_TOKEN unset
uv run python scripts/setup_dev.py --skip-sync   # stack + schema only
```

It waits for postgres to become ready, applies the schema, and syncs the
catalog; steps are idempotent and safe to re-run. Otherwise, manually:

1. Start the stack (Docker Desktop must be running):

   ```bash
   docker compose up -d postgres redis
   ```

2. Create `.env` from `.env.example` and fill in:

   ```dotenv
   HETZNER_API_TOKEN=...      # from the Hetzner Cloud console
   TELEGRAM_BOT_TOKEN=...     # from @BotFather
   CALLBACK_SIGNING_KEY=...   # any long random string (button signatures)
   ```

3. Apply the schema and sync the Hetzner catalog (idempotent):

   ```bash
   uv run alembic upgrade head
   uv run python scripts/sync_catalog.py
   ```

4. Run the bot:

   ```bash
   uv run python -m cloud_platform.bot.main
   ```

   `/start` greets in Persian; `/menu` opens the main menu;  خرید سرور
   walks the full buy flow: datacenters -> plans -> OS image -> price
   confirmation (exact wallet impact) -> order. Every button carries a
   signed M08-001 callback; tampered/expired buttons show a notice, and
   pressing  تأیید و پرداخت twice replays the same idempotent order
   instead of double-charging.

5. Verify a sync in the DB (optional):

   ```bash
   uv run python -c "from cloud_platform.core.container import create_container; import asyncio; c=create_container(); print(asyncio.run(c.catalog_repository().list_offers()))"
   ```

**Buy-flow guardrails:** the OS list is architecture-gated server-side
(M08-004); the confirmation screen shows the exact price policy + hold
impact (M08-005); the order itself is the idempotent create command — the
same one the REST v1 will use — so a double tap never charges twice. The
confirmation price requires an active price-book version for
`PRICE_BOOK_NAME`; without one, orders fail cleanly with a friendly
message.

**Next increments:** My Servers (list/detail/power) and wallet/recharge
screens; provider account linking so orders can actually provision.

## Selling from Hetzner + LeaseWeb (M17)

Both EU providers sell through the same bot/API surfaces. The launch
sequence on any environment (local, staging, VPS via `./platform.sh`):

1. **Keys in `.env`** — `HETZNER_API_TOKEN` and/or `LEASEWEB_API_KEY`
   (plus `ARVANCLOUD_*` for the Iranian track, `ZARINPAL_*` for payments).
   Never commit `.env`; staging keeps its own (see
   `deploy/staging/.env.example`).
2. **Schema** — `uv run alembic upgrade head` (or `migrate` in compose;
   single head `0029`, hot-path indexes included).
3. **Catalog sync** — `uv run python scripts/sync_catalog.py` (all
   providers) or `--provider hetzner|leaseweb|arvancloud`. Idempotent;
   LeaseWeb needs its API key, otherwise that step fails loudly while the
   others still sync. Live LeaseWeb price fields are confirmed in
   `docs/leaseweb/INTEGRATION_NOTES.md`.
4. **Price book** — publish at least one active margin version or every
   confirmation/order fails cleanly:
   `uv run python scripts/publish_price_book.py --reason "..."
   --admin-telegram-id <id> --bootstrap-admin`
   (or item 12 in `./platform.sh`). Rules are explicit per
   provider/plan/location with `margin_factor` > 0; no provider price is
   ever hard-coded.
5. **Offers** — enable exactly the plan/location rows to sell (offer
   visibility; disabled rows never appear in the bot or REST).
6. **Bot** — `/menu` → buy flow (datacenter → plans → OS → exact price +
   wallet impact → idempotent order), My Servers (power/rebuild), wallet
   + ZarinPal top-up (pending/success/failure screens; stuck sessions are
   rechecked by the `reconcile_payments` worker job).
7. **Smoke** — `uv run python scripts/post_deploy_smoke.py`, then a real
   1-quantum order and delete (final charge posts exactly once).

**Kill switches before launch:** maintenance block per provider/location
(M10-005), cost circuit breaker (M10-004), per-user freeze with resource
containment (M10-007), and the game-day drill (M16-008).

## Monthly Leaseweb VPS storefront (LEASEWEB-MVP)

The MVP sells fixed-price **prepaid monthly** Leaseweb VPS products
(`billing_model = prepaid_monthly_fixed`) through the Telegram bot, backed
by the ordering API. Full design: `docs/leaseweb/MVP_DESIGN.md`; provider
contract: `docs/leaseweb/PROVIDER_CONTRACT.md`.

### Launch sequence (monthly track)

1. **Env** — set in `.env` (or `deploy/staging/.env` with the `STAGING_`
   prefix): `LEASEWEB_API_KEY`, `LEASEWEB_LOCATIONS=AMS-01,FRA-01`,
   `TELEGRAM_BOT_TOKEN`, `CALLBACK_SIGNING_KEY`, `TELEGRAM_ADMIN_CHAT_ID`.
   `LEASEWEB_ALLOW_LIVE_ORDER_TEST` stays `false` until the controlled
   first-order procedure below.
2. **Schema** — `uv run alembic upgrade head` (head `0030`).
3. **Doctor** (read-only pre-flight, never prints the key):
   `uv run python -m cloud_platform.cli leaseweb doctor`
4. **Sync the price book**:
   `uv run python -m cloud_platform.cli leaseweb sync-offers`
   (also runs daily via the worker at 03:17 UTC). Sync refreshes provider
   cost/specs and availability only — it never enables or prices anything.
5. **Enable + price the first offer**:
   - `uv run python -m cloud_platform.cli offers list --all` (find the id)
   - `uv run python -m cloud_platform.cli offers price <offer_id> 1299 EUR`
   - `uv run python -m cloud_platform.cli offers enable <offer_id>`
   The offer is sellable only when provider-reported AND enabled AND priced.
6. **Bot** — `/menu` → خرید سرور (location → plan → OS → exact monthly
   price → confirm). `سرورهای من` shows only the user's own servers with
   power controls behind explicit confirmation; `کیف پول` shows balance +
   ledger history; `پشتیبانی` shows the support contact.
7. **Wallet funding** — manual for the MVP:
   `uv run python -m cloud_platform.cli users find <telegram_id>` then
   `uv run python -m cloud_platform.cli wallet credit <user_id> <minor> "<reason>"`
   (immutable ledger entry + audit; never ad-hoc SQL).
8. **Smoke** — the worker crons submit/reconcile orders automatically
   (`process_leaseweb_orders` every 2 min, `reconcile_leaseweb_orders`
   every 3 min); watch `uv run python -m cloud_platform.cli orders list`
   and `orders attention`.

### Controlled procedure for the FIRST real paid order

1. Complete steps 1–5 above; the customer account is funded.
2. Pre-verify the offer end-to-end WITHOUT billing: `leaseweb doctor`
   green, `offers list` shows the offer as `SALE`.
3. Have the customer (or a test account) complete the bot flow and confirm.
   The wallet hold is created; `orders list` shows a `pending_submit` row
   **before** any provider call.
4. The worker POSTs the order and persists the provider order id; the
   wallet is debited exactly once on acceptance. Track it:
   `uv run python -m cloud_platform.cli orders inspect <order_id>`.
5. The reconciler polls until the VPS is discoverable, then delivers the
   server card (IPs, OS, renewal date) to the owning chat and creates the
   renewal record.
6. Verify in the Leaseweb portal that exactly ONE order/service exists.

To place a REAL order outside the bot flow (diagnostics only, NOT linked
to a platform server): `LEASEWEB_ALLOW_LIVE_ORDER_TEST=true
uv run python -m cloud_platform.cli leaseweb smoke-order --offer <id>
--os-index 0 --yes`. Without the env switch and `--yes` it refuses.

### Renewal and cancellation runbook

Leaseweb renews services automatically at ITS billing cycle; the daily
`check_renewals` job (03:23 UTC) protects us:

- **T-7d / T-3d / T-1d** — customer reminders (exactly once per period);
  insufficient balance is flagged on the renewal record at T-3d, and the
  admin chat is alerted at T-1d.
- **On the renewal date with sufficient balance + auto-charge enabled** —
  exactly one monthly debit (idempotency key `renewal-charge:{server}:{date}`);
  the customer is told the next renewal date.
- **Insufficient balance on the due date** — status
  `INSUFFICIENT_FUNDS`; after 1 day grace it becomes
  `MANUAL_CANCELLATION_REQUIRED` and the admin alert names the provider
  order/contract/server ids.
- **Cancellation is a MANUAL portal operation** (the current Leaseweb VPS
  API has no verified cancel endpoint — the MVP never invents one). For
  every `MANUAL_CANCELLATION_REQUIRED` record:
  1. `uv run python -m cloud_platform.cli renewals list --attention`
  2. Cancel the service in the Leaseweb Customer Portal using the
     provider order/contract references from the alert.
  3. Confirm with the provider that renewal is off.
  4. After cancellation, contact the customer (funds were already spent
     only for elapsed periods; no refund is automatic).

Attention queue summary at any time:
`uv run python -m cloud_platform.cli orders attention` (failed/needs-review
orders) and `renewals list --attention` (unpaid-risk services).
