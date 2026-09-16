# Leaseweb multi-credential-account operation

A Leaseweb API key belongs to one **Sales Organization**, which only sees a
narrow set of datacenters. Selling a wide footprint therefore needs several
keys — but customers must still see **one** provider called `Leaseweb`.

```
customer sees:    Leaseweb  ->  Frankfurt, Amsterdam, Singapore
infrastructure:   leaseweb/lw-eu    -> Frankfurt, Amsterdam
                  leaseweb/lw-asia  -> Singapore
                  leaseweb/lw-us    -> (401, auth failed)
```

The domain, the catalog and the storefront keep using the single provider key
`leaseweb`. Only the infrastructure layer knows which API key fulfills a given
resource.

## 1. Adding a key

1. Edit the **server-owned** `configuration.toml` (mounted read-only at
   `/etc/cloud-server-seller/configuration.toml`). Append:

   ```toml
   # The table key IS the account id — nothing repeated, nothing to drift.
   [providers.leaseweb.accounts.sales-org-asia]
   api_key = "..."            # never committed, never printed
   enabled = true
   label = "Singapore Sales Organization"   # optional, operator-facing only
   priority = 200
   ```

   The equivalent list form (`id` as a field) is also accepted, and an account
   may be declared at the document root as `[leaseweb.accounts.<id>]`. A keyed
   table that states an `id` disagreeing with its key is refused at
   configuration load: the id addresses every server and order that account
   ever created, so a silent rename would orphan them.

2. Restart / redeploy the application services.

Nothing else. The account is authenticated, its eligible locations are
discovered from live read-only provider responses, its products are synced, and
its locations appear in the existing Leaseweb storefront. **No code change, no
GitHub variable, no location declaration, no database insert.**

Keys stay out of the deployment workflow on purpose: `docker-compose.yml`,
`deploy.env` and CI variables never contain provider credentials.

## 2. Field reference

| Field | Meaning |
| --- | --- |
| `id` | Stable, **non-secret** handle (`[A-Za-z0-9._-]`, 1–63 chars). Appears in logs, CLI output, `provider_routes` and order/server rows. It survives key rotation; the credential does not. In the keyed TOML form it is the table name. |
| `api_key` | The provider credential. Redacted everywhere (repr, logs, CLI, exceptions). |
| `label` | Optional human name for operators; `leaseweb accounts doctor` prints it, and it falls back to `id`. Never customer-visible, never a secret, never used for routing. |
| `enabled` | `false` takes the account out of service completely: no discovery, no orders, no management of what it owns. To stop *new sales* while still managing the servers the account already provisioned, keep `enabled = true` and set `state = "draining"`. |
| `priority` | Lower wins when several accounts can serve the same location. Selection is deterministic: `(priority, id)`. |
| `state` | See below. |

`locations` is deliberately absent: locations are **discovered**, never
declared. `[providers.leaseweb] locations` remains a *discovery seed* hint for
the whole provider, not an allowlist.

### Account lifecycle

| `state` | Discovery | New orders | Manages what it owns |
| --- | --- | --- | --- |
| `active` | yes | yes | yes |
| `draining` | yes | **no** | yes |
| `disabled` | no | no | **no** (fail closed) |

Draining an account is the supported way to stop selling through it *without*
stranding the customer servers it provisioned.

## 3. Backward compatibility

The deprecated single credential keeps working unchanged:

```toml
[providers.leaseweb]
api_key = "..."
```

Settings normalize it into one account with id `default`, which is exactly the
account id the migration backfills onto pre-existing Leaseweb servers and
orders. Nothing becomes un-routable, and no operator action is required.

Mixing the two forms is not needed: if `[[providers.leaseweb.accounts]]` blocks
exist, the flat `api_key` is ignored.

Configuration is refused at start-up when it is self-contradictory:

- an invalid or duplicate account `id`;
- an unknown `state`;
- an `enabled` account with an empty `api_key`;
- accounts configured but none enabled.

## 4. Routing and pinning

For every location, each account records a **durable observation** in
`provider_routes` (`eligible_available` / `eligible_empty` / `ineligible` /
`transient_unknown` / `auth_failed` / `disabled`) with its priority and the
product ids it currently reports.

Which account serves a purchase is decided **once**, at checkout, and then
frozen:

```
checkout -> select eligible route (priority, id) -> PIN credential_account_id
         -> server + provider order + operation + wallet hold
         -> worker: POST through THAT account only
```

Order selection policy, in order: currently eligible+available → accepting new
orders → reports the product → tie-break `(priority, id)`. There is no
"cheapest provider cost" rule: routing on provider cost would silently reprice
customer offers.

If the provider has credential accounts but none serves the location/product,
the purchase is **refused**. The platform never guesses an account.

The same pin is authoritative afterwards:

- order submission, polling and recovery use the pinned account;
- account-order inventories are credential-scoped, so a recovery scan searches
  **only** that account;
- every management operation (power, IPs, snapshots, reinstall, console,
  metrics, monitoring, credentials) is addressed with the owning account's key;
- an existing server never migrates to another account when routing changes.

## 5. Never a cross-account retry

```
order pinned to lw-2 -> POST transmitted -> outcome ambiguous
```

The order enters `OUTCOME_UNKNOWN` and is resolved by read-only scans through
**lw-2** only, or escalated to a human. It is never re-POSTed, and never
through another Leaseweb account: that could buy two VPSes. The existing
at-most-one-billable-POST invariant is unchanged.

## 6. Failure isolation

| Failure | Effect |
| --- | --- |
| One account times out / 429 / 5xx | That account keeps its **last-known** sellability; recorded as `transient_unknown`. |
| One account gets a definitive 403 | That account's route for the location becomes `ineligible`; **other accounts' locations are untouched**. |
| One account gets 401 | Only that account is `auth_failed`. The provider stays operational. |
| Every account 401s | No provider evidence at all → availability is **left completely untouched** and the run reports an error. A broken deployment never empties the storefront. |
| An account id is deleted while resources depend on it | Those resources **fail closed** with an explicit diagnostic; they are never routed through another key. |

A location is hidden from the storefront only when **every** account that knows
it gave a definitive negative answer. If any account's view is unknown,
last-known availability is preserved.

## 7. Prices

Provider cost may differ between Sales Organizations. The active route's
observed cost becomes the durable order's provider-cost snapshot, and a
disagreement between accounts is logged (account ids only, never credentials).

The **customer selling price is operator-owned** (`sellable_offers.enabled`,
`selling_price_minor`) and is never written by a provider sync, whatever the
accounts report.

## 8. Operator commands

```bash
# safe inventory: id, enabled, state, priority, auth verdict, served locations
python -m cloud_platform.cli leaseweb accounts list

# per-credential authentication AND per-location product counts
python -m cloud_platform.cli leaseweb accounts doctor

# refresh the price book from EVERY account, reporting what each one served
python -m cloud_platform.cli leaseweb sync-offers

# full pre-flight (adds per-account auth + the default account's locations)
python -m cloud_platform.cli leaseweb doctor
```

### 8.1 "The customer catalog is empty" — sync is only half the job

An offer reaches a customer through **five** gates. A sync clears only one of
them, so a freshly synced catalog is *expected* to be invisible:

| Gate | Cleared by |
| --- | --- |
| provider has a `market` | `[providers.leaseweb] market = "..."` |
| provider is `enabled` | `[providers.leaseweb] enabled = true` |
| provider implements ordering | built-in (no config) |
| offer `provider_available` | **catalog sync** |
| offer `enabled` | operator (`offers enable`) |
| offer `selling_price_minor > 0` | **operator (a sync never sets a price)** |

So the go-live sequence is sync **then price**, never sync alone:

```bash
python -m cloud_platform.cli leaseweb sync-offers
# ... then, when the report says "NOTHING is on sale":
python -m cloud_platform.cli offers price-book --provider leaseweb --markup-percent 30
python -m cloud_platform.cli offers doctor    # every gate, with counts
python -m cloud_platform.cli offers preview   # the catalog as the BOT builds it
```

`offers price-book` prices only offers that have **no** price yet, in the
currency the provider cost was captured in (the markup is an integer
percentage you choose; it never relabels currency, so it can never imply an
exchange rate). `--dry-run` shows the effect first. A provider cost is not a
selling price — `--markup-percent 0` is legitimate but is then a deliberate
choice, not a default.

`offers doctor` names the failing gate (with counts) and the exact command
that clears it; `offers preview` walks the same view service as the Telegram
storefront, so it shows precisely what a customer would see — including
nothing.

`accounts doctor` prints, per credential, what that key alone can sell where —
and never a key:

```text
[OK ] Leaseweb accounts configured — 2 configured
[OK ] credential North Org authenticated
[OK ] AAA-01 products: 6
[OK ] BBB-02 products: 6
[WARN] detail endpoint unavailable for BBB-02
[OK ] credential UK Org authenticated
[OK ] CCC-03 products: 6
[OK ] Leaseweb aggregate catalog — 2 usable account(s)
Summary:
credentials: 2
locations: 3
offers: 18
```

`sync-offers` prints the same evidence per sync step:

```text
products: fetched=18 upserted=18 skipped=0
  account north: AAA-01: 6 products, BBB-02: 6 products
  account uk: CCC-03: 6 products
  warning/error: detail north/VPS02_1/BBB-02: LeasewebServerError
```

A `detail` line there is a **warning**: the product stays on sale (see §13).

No command prints an API key, not even partially.

Both `accounts` commands also warn when an account that used to exist is gone
from configuration while resources still point at it:

```text
WARNING: credential account 'lw-2' is missing from configuration but 14 existing
resource(s) depend on it (12 server(s), 2 provider order(s)); those resources
fail closed until the account is restored
```

`leaseweb accounts doctor` reports `DEGRADED` (exit 0) while at least one
account works and `UNAVAILABLE` (exit 1) only when none does, so a single
revoked key does not page the operator as an outage.

## 9. Observability

Safe dimensions in logs: `provider=leaseweb`, `credential_account=lw-eu`,
`location=AMS-01`. Never `X-LSW-Auth`, never an `Authorization` header, never a
key. Business-log events stay customer-facing and carry no account ids.

## 10. Credential rotation

Runtime rotation addresses one account: `(provider_key, credential_account_id)`.
Rotating `lw-eu` swaps only `lw-eu`'s holder, after verifying the candidate
read-only against `lw-eu`'s own adapter. The stable account id survives the
rotation, so no order, server or route needs to change.

## 11. Data model

| Store | Purpose |
| --- | --- |
| `provider_routes` | durable `(provider, account, location)` eligibility + priority + observed products. Unique per triple, so a sync run is idempotent. |
| `sellable_offers.provider_account_id` | the credential an offer was **discovered through** (provenance; never a key, never customer-visible). One offer row per `(provider, location, product)` — the thing the operator prices and the customer buys — while the per-account inventory identity lives in `provider_routes`. |
| `provider_orders.credential_account_id` | the account PINNED before the chargeable POST |
| `servers.credential_account_id` | the account that owns the VPS |

### Catalog identity

The sellable inventory item is

```
(provider_account_id, location, product_id)
```

— **not** `product_id` alone. The same product in two datacenters, or seen by
two keys, is a different thing to sell:

```
VPS02_1 + AAA-01   <- one offer
VPS02_1 + BBB-02   <- another offer
VPS02_1 + CCC-03   <- another offer
```

The per-account half of that triple is durable in `provider_routes` (one row per
account + location, carrying the products that account sells there);
`provider_routes` also decides which account fulfills a purchase for a given
location, deterministically by `(priority, id)`. Because several accounts can
legitimately serve one datacenter, the *customer-visible* offer row stays unique
per `(provider, location, product)` — one price, one card, no duplicates — and
records its discovering credential in `provider_account_id`.

## 13. Which endpoint decides the catalog

```
GET /ordering/v1/products/vps?location={location}          <- SOURCE OF TRUTH
GET /ordering/v1/products/vps/{productId}?location={...}   <- OPTIONAL ENRICHMENT
```

The location-scoped **list** endpoint decides which products this credential may
sell where, and its row is enough on its own (name, spec, price). The
per-product **detail** endpoint only enriches that row (contract-term price,
full configuration, OS options) and is known to answer **HTTP 500 for locations
whose list endpoint works normally** — so a detail failure is recorded as a
warning and the product stays on sale on the list row's data:

| Detail outcome | Effect on the catalog |
| --- | --- |
| 200 | row enriched (contract-term price, spec, available locations) |
| 403 / 404 / 500 / timeout / malformed | product **kept** from the list row, warning recorded |

`leaseweb accounts doctor` probes one product per discovered location so an
operator can see this condition before a customer notices anything.

## 12. Deleting a credential account

Deleting an account from `configuration.toml` is **not** a supported way to
retire the customer servers it created, and the platform will not pretend it
is:

- every row it pinned keeps its account id (nothing is re-pinned, ever);
- power, reinstall, credentials, IP and reconciliation calls for those
  resources **fail closed** with an explicit diagnostic instead of being
  addressed with a key that does not own them;
- `leaseweb accounts list` and `leaseweb accounts doctor` report how many
  servers and provider orders depend on the missing account id, so the operator
  sees the blast radius before a customer does;
- if the dependency check itself cannot run (database unreachable), that is
  reported as *unavailable*, never as a clean result.

To retire an account properly: set `state = "draining"` first (no new sales,
existing resources fully manageable), migrate or terminate what it owns through
the normal customer-facing flows, and only then remove the entry.

Both columns hold the stable non-secret account id. No API key is ever
persisted. Existing rows are backfilled to `default` by migration `0035`.
