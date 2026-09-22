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
snapshots and are never derived from each other; both are integer minor units,
never float.

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
* Hetzner: `[providers.hetzner]` with an API token (`hetzner_api_token`).
  Tokens live only in server-owned configuration — never in Git, compose files,
  deploy env or logs.

## Nothing here mutates a provider

Every command in this runbook is READ-ONLY at the provider. Billable calls
(server creation/order placement) happen only through the durable pipeline:
Telegram checkout -> wallet hold -> operation ledger -> worker. There is no CLI
or test command that can create a live server.
