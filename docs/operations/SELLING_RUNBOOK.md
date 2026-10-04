# Selling runbook — provider-neutral storefront

This is the operator path from "the provider has inventory" to "a customer can
buy it in Telegram". It applies to every provider (Leaseweb, Hetzner, any future
adapter) because the storefront decides everything from the provider PORT, not
from a provider name.

## SYNC != PRICE != ENABLE

These are three deliberately separate actions, performed by three different
actors. They can never happen by accident:

| # | Action | Who | Effect |
|---|--------|-----|--------|
| 1 | **SYNC** the catalog | scheduled/operator command | writes provider **cost** + specs + availability |
| 2 | **PRICE** the offer | operator, explicit markup | writes the customer **selling price** |
| 3 | **ENABLE** the offer | operator, explicit | flips the visibility switch |

A synced offer arrives **disabled and unpriced** (`enabled = false`,
`selling_price_minor = 0`) — safe by default. A catalog refresh therefore can
never put a newly discovered provider product on sale, and a re-sync never
overwrites an operator price. `provider cost` and `selling price` are separate
snapshots. Customer prices use integer minor units; provider-native currency and
exact hourly rates are preserved until the customer-currency rounding boundary.
Financial arithmetic never uses float.

An offer is customer-visible only when ALL THREE gates are open:
`provider_available` **and** `enabled` **and** `selling_price_minor > 0`.
`offers doctor` names the first closed gate for every row.

## 0. Release the schema first (deploy, never a hand-run upgrade)

Application code and the database schema are released **together**. The deploy
script refuses to start `api`/`worker`/`bot` unless the database reports exactly
the alembic head the release image ships:

```
preflight  : pull image -> configuration accepted by the image -> IMAGE head read FROM THE IMAGE
             (EXPECTED_HEAD from the pipeline is cross-checked against it; two heads -> refuse)
pull       : release image
migrate    : compose candidate `migrate` service -> `alembic upgrade head`
gate 1     : DB head == IMAGE head, otherwise FAIL before any service is replaced
gate 2     : DB PHYSICAL schema matches the release, otherwise FAIL before any service is replaced
start      : api + worker + bot (exactly one bot)
```

Gate 2 exists because **revision equality is not schema compatibility**. Production
proved it: the database reported revision `0037` while objects created by
migration `0035` (`provider_routes`, `provider_orders.credential_account_id`,
`servers.credential_account_id`) were physically absent. Gate 1 passes on such a
database; gate 2 is what stops it. It reflects the database and checks, for every
table and column the release's models declare, that the table/column exists and
that the uniqueness its upserts need is enforced (matched by columns, not by name).
It never writes, never migrates and never stamps — a drifted database is repaired
forward by the `migrate` stage above.

Read-only checks (safe to run at any time; none of them mutates the database):

```bash
docker compose --env-file deploy.env -f docker-compose.yml run --rm --no-deps migrate alembic current
docker compose --env-file deploy.env -f docker-compose.yml run --rm --no-deps migrate alembic heads
docker compose --env-file deploy.env -f docker-compose.yml run --rm --no-deps migrate python -m cloud_platform.db.schema_parity
```

Interpretation: `current` must equal `heads`, and both must equal the head the
running image was built with. `heads` printing **two** revisions means the
repository has an un-merged migration fork — deploy is blocked until it is merged.
`schema_parity` exits 0 with `[ok  ] physical schema matches the release …`, or
non-zero and lists every missing table/column — the same check the deploy runs.

Upgrades happen **only** through the deploy script's `migrate` stage. Never run
`alembic stamp` to make the database claim a revision: stamping turns a missing
schema into an invisible one, which is exactly how `relation "provider_routes"
does not exist` reached production (`provider_routes` is created by migration
`0035`; the routing code that queries it ships in the same release).

## Reading a sync report

`leaseweb sync-offers` prints DISCOVERED and PERSISTED separately, because they
can differ:

* `discovered` — observations the provider returned (the location-scoped LIST;
the per-product DETAIL never counts as discovery).
* `persisted` — offer rows actually written; `routes` — routing observations
  written (these are what let checkout pin a fulfillment account).
* `PERSISTENCE FAILURE:` — a durable write failed. The command then prints
  `FAIL: the catalog was read from the provider but NOT persisted`, returns a
  non-zero exit status, and **retires nothing**: a run whose writes failed has
  an incomplete view of the catalog, so it must not mark offers unavailable.
  The usual cause is a database behind the image (see step 0).

A detail-endpoint outage is NOT a persistence failure: it is reported per
location and the list-discovered product stays on sale.

## 1. Sync the catalog (READ-ONLY at the provider)

```bash
# Leaseweb: every configured credential account, merged into one catalog
python -m cloud_platform.cli leaseweb sync-offers

# Hetzner: locations + the server types offered at each location
python -m cloud_platform.cli hetzner sync-offers
```

Both commands print per-location product counts, isolate a failing credential or
location (the others still sync), and end with a **Storefront readiness** block
that says out loud whether anything is now sellable.

For the catalog, the location-scoped LIST endpoint is the source of truth.
The per-product DETAIL endpoint is only ever optional enrichment: a 403/404/500/
timeout there keeps the product in the catalog with a warning.

## 2. Price the offers (operator-owned)

```bash
python -m cloud_platform.cli offers price-book --provider hetzner \
    --markup-percent 30 --dry-run      # show the effect first
python -m cloud_platform.cli offers price-book --provider hetzner --markup-percent 30
```

`price-book` prices **only unpriced offers** and never touches an existing
price, never changes currency, and rounds up so a product cannot be sold below
its provider cost. Pricing one offer explicitly is also available:

```bash
python -m cloud_platform.cli offers price <offer_id> <minor_units> <CURRENCY>
```

## 3. Enable the offers

```bash
python -m cloud_platform.cli offers list --all      # ids + gate status
python -m cloud_platform.cli offers enable <offer_id>
```

## 4. Verify what the customer will see

```bash
python -m cloud_platform.cli offers doctor                  # which gate is closed, per provider
python -m cloud_platform.cli offers preview --market foreign # the exact storefront view
```

`offers preview` walks the SAME view service the bot uses, so it prints what the
customer sees (product cards, monthly price, locations) without Telegram.

## Post-deploy acceptance (all READ-ONLY)

Price and enable (steps 2 and 3) only after every row below holds:

| # | Check | Expected |
| --- | --- | --- |
| 1 | `alembic current` | equals `alembic heads` — the head the image ships |
| 2 | `python -m cloud_platform.db.schema_parity` | exit 0 — `provider_routes` exists with its uniqueness, and `provider_orders.credential_account_id` / `servers.credential_account_id` exist |
| 3 | `leaseweb accounts doctor` | each configured account reports its own locations and product counts; `DEGRADED` (not `UNAVAILABLE`) when one fails |
| 4 | `leaseweb sync-offers` | exit 0, zero `PERSISTENCE FAILURE` lines |
| 5 | same run | `FRA-*` offers carry `EUR` + the German account, `LON-*` offers carry `GBP` + the UK account |
| 6 | `offers doctor` | reports the remaining `disabled`/`unpriced` gates, and no stale legacy provenance |
| 7 | `offers preview --market foreign` | product cards with per-location availability |

A drifted database is repaired by the deploy's migration stage (migration
`0038`, see `docs/leaseweb/MULTI_ACCOUNT.md` §14): a missing `provider_routes` is
created **empty** and the next sync populates it, while a drifted database whose
historical rows belong to a multi-account provider fails that migration on
purpose instead of guessing which key owns them.

## Command map by provider

| Provider | sync | diagnose |
|---|---|---|
| Leaseweb | `leaseweb sync-offers` | `leaseweb accounts doctor`, `leaseweb doctor`, `offers doctor` |
| Hetzner | `hetzner sync-offers` | `hetzner doctor`, `offers doctor` |

## How each provider provisions (why the storefront accepts both)

* **order-based** (`ORDER_BASED`) — Leaseweb: place an order, poll it, then match
  the delivered VPS. Checkout and the worker use the ordering port.
* **direct-create** (`DIRECT_CREATE`) — Hetzner: create the server directly and
  reconcile its state. No order is faked; the worker calls ordinary server
  creation through the provider port.

Both modes share the same financial pipeline: explicit offer reload, wallet
hold, durable operation identity, provider-cost snapshot, and settlement
(hold capture + exactly one ledger charge) BEFORE delivery. A customer selling
price is never sent to a provider.

### Ambiguous create safety (direct-create)

A timeout or drop after the create POST is recorded as `OUTCOME_UNKNOWN`: the
hold stays reserved and **no second POST is ever sent**. Resolution is
READ-ONLY — the platform looks the resource up by its own operation label
(`platform-operation`). Exactly one provable match attaches and continues;
zero or several candidates escalate to human review. Two servers are always
worse than a review.

## Requirements per provider

* Leaseweb: `[providers.leaseweb.accounts.<id>]` credential accounts (see
  `docs/leaseweb/MULTI_ACCOUNT.md`).
* Hetzner: `[[providers.hetzner.accounts]]` or keyed
  `[providers.hetzner.accounts.<id>]` entries with stable `id`, `api_token`,
  `enabled`, `priority`, `state`, optional `label` and optional `server_limit`.
  The legacy `[providers.hetzner] api_token` becomes account `default` only when
  explicit accounts are absent; an explicit empty list is authoritative.
  Tokens live only in server-owned configuration — never in Git, compose files,
  deploy env or logs.

### Hetzner capacity and ownership

Use `active` for new purchases, `draining` for managing/reconciling existing
resources without new purchases, and `disabled` to exclude the credential.
Keep IDs stable. When migrating from the single token, retain the original
credential as an explicit `default` account if any NULL/default-owned resources
still exist. Removing it fails closed; the preferred account never substitutes
for a missing owner.

`GET /servers` counts the complete token Project, including powered-off and
manually created resources. `server_limit` is an operator-confirmed Project
ceiling, not an API-discovered account-wide limit. Omit it when unknown; doctors
report no configured ceiling, not unlimited capacity. Multiple tokens for the
same Project, or Projects sharing an owning-account quota, do not establish
independent capacity.

Read-only diagnostics:

```bash
python -m cloud_platform.cli hetzner doctor
python -m cloud_platform.cli hetzner cloud doctor
python -m cloud_platform.cli catalog auto-sync doctor
```

The two billing families publish separate offers but share independently
observed product/location routes. A known-full preferred Project is skipped.
Every selected alternative must independently prove the original native
cost/currency, product/location and OS/image; hourly creation also proves the
frozen root disk. Inventory/read failures mean unavailable, not "all full", and
must not retire the catalog. Catalog refresh never changes accepted contracts.

Native currency is read from each credential's official `GET /pricing` response,
at `pricing.currency`. The location's `/server_types` price entries do not carry
currency. Account currency may differ; missing/unsupported currency is unavailable,
never an EUR/default assumption. Currency reads scale with credential passes, not
offer count; every candidate re-proves the accepted native currency before POST.

The worker records each account as `sent` before POST. Hetzner's documented
HTTP 403 `resource_limit_exceeded` can become `capacity_refused` and continue
the **same** intent/hold/snapshot on another eligible account. Transport loss,
408/429/5xx, `422 service_error`, malformed success and unproven rejection remain `outcome_unknown`:
no alternative POST, no release of a monthly reservation. Recovery scans only
the attempted credential using the bounded operation label; no match or an
ambiguous/foreign match remains unresolved.

HTTP 412 `resource_unavailable` is a definitive non-capacity refusal, not permission
to try another account. A crash after committing a terminal refusal can be fenced
and finalized without a new POST; only then may a monthly reservation be released.
Historical attempted pending/in-flight intents without receipts use their original
account and exact legacy correlation for read-only recovery; never reconstruct
cross-account authorization from their current catalog.

For monthly orders, accepted provider identity alone does not unlock provisioning
or activation. Failed/pending local settlement leaves the server `requested` with
the accepted identity intact. The existing capture and ledger proof must succeed
before the server can leave that barrier.

Legacy bot intents are handled by the legacy provisioning worker inside the
existing create job; modern versioned hourly and prepaid monthly intents have
their own owners. There is no second poller or new infrastructure. Terminal
Telegram buttons reference an owner-bound shared-store confirmation, not a
mutable image index. Changed prices, expired references and store outages fail
closed; repeated valid clicks replay the original intent.

Receipt storage uses existing operation/server/order JSON and pin columns.
No schema migration is introduced. Real lock/rollback acceptance requires
`CLOUD_PLATFORM_TEST_POSTGRES_URL` on an isolated test PostgreSQL instance and:

```bash
uv run pytest tests/live/test_postgres_hetzner_account_attempts.py
```

The fixture creates and drops only its generated scratch database; its test
principal must have that permission. Never point it at the production database.

## Customer identity and payments before the first sale

Release revision `0050` through the existing migrate/deploy pipeline before
starting the new application. It adds nullable phone/identity fields,
gateway settings, safe invoice details, the AtlasPay attempt uniqueness guard
and the hourly activation anchor. Existing customers must complete identity
before another server purchase; accepted server price snapshots are unchanged.

Checkout requests a private-chat Telegram **contact keyboard**. The contact
must belong to the sender's own Telegram account, must not be forwarded, and
must normalize to an Iranian mobile number (`+989xxxxxxxxx`). Text-only phone
input and another account's contact are rejected. Then the customer supplies
a checksum-valid ten-digit Iranian national ID. This proves Telegram contact
ownership and validates ID structure; it does **not** perform legal
phone-to-national-ID verification through Shahkar or another identity registry.
The owner-bound pending purchase survives a bot restart and resumes after
verification. Phone/national-ID values must not appear in logs.

Configure AtlasPay in the server-owned configuration:

```toml
[payments.atlaspay]
enabled = true
api_key = "<provision through secret storage>"
base_url = "https://api.atlaspay.space/api/v1"
timeout_seconds = 30
```

Use the official URL/configuration example shipped with this release; never
put the real key into the public repository. AtlasPay takes integer Toman
(`IRT`) order amounts between 50,000 and 2,000,000. The displayed invoice
total comes from `totalAmountToman`, including provider fees; the frozen
wallet credit and FX snapshot remain the amount accepted by the customer.
Tracking/deadline/link are shown; internal order IDs or bank/card facts are not.

Foreign storefront wallets use the canonical catalog currency (`USD`). AtlasPay
can issue a new invoice only when the configured FX policy supplies an
authoritative `USD`→`IRT` charge conversion. Check that conversion before the
first sale; provider-native currency conversion and wallet settlement are
different operations. An unavailable/stale rate hides new invoices, not pending
settlement. Never invent a 1:1 rate or silently enable USDT-proxy settlement.

AtlasPay has no assumed merchant-reference idempotency guarantee. The platform
persists a unique attempt before its single creation POST. A lost/ambiguous
creation response remains unresolved and cannot trigger another POST. Check
that attempt with AtlasPay before any operator action; do not ask the customer
to pay a guessed invoice. Paid/confirmed authoritative inquiry must match the
invoice identity, merchant reference and amount before atomic credit.
Underpayment/manual-verification responses remain pending.

Active super-admins use `/admin` to switch configured gateways or credit a
registered customer manually. A disabled gateway cannot create new invoices;
existing invoices still settle. Manual credit requires target, amount, reason
and confirmation, with one atomic wallet/ledger/audit/business-outbox commit.
Self/admin credit and super-admin self recharge are unavailable.

## Hourly resources buy their next hour in advance

Checkout reserves the first hour at the immutable selling-price snapshot.
A definitive pre-activation create failure releases that reservation; an
ambiguous provider outcome does not. Provider readiness establishes the exact
`billing_started_at` instant and captures the first hour before delivery or
power actions. `last_accrued_at` is now the exclusive **paid-through** instant,
not a completed-usage watermark. Real PostgreSQL advisory locking and stable
ledger keys prevent duplicate capture or stale-worker re-anchoring.

The billing worker schedules each next purchase at paid-through minus five
seconds in the existing ARQ queue; the periodic job recovers missing schedules.
If the next hour is unaffordable early, the already-paid hour remains valid
and the worker retries at its boundary. At an unaffordable boundary it requests
the existing reconciled delete saga, not an unpaid grace period. Low-balance
thresholds warn but do not revoke paid time.

Stopped resources continue to incur charges until provider deletion is
confirmed. Deletion posts no extra trailing hour and does not refund an
already purchased hour. Existing provider-bound hourly rows retain their old
anchor during migration; frozen customer prices and native provider rates
are never rebuilt from current catalog/FX facts.

First-sale checks after an authorized deploy: customer contact/ID → AtlasPay
invoice/status → exactly one wallet credit → approved smallest server →
first-hour capture before readiness → next-hour debit before coverage expires
→ deletion reconciliation. Local real-PostgreSQL/Redis checks use synthetic
provider resources and official-shaped payment transports; they are not a
claim that a real payment or billable provider order was performed.

## Nothing here mutates a provider

Every command in this runbook is READ-ONLY at the provider. Billable calls
(server creation/order placement) happen only through the durable pipeline:
Telegram checkout -> wallet hold -> operation ledger -> worker. There is no CLI
or test command that can create a live server.
