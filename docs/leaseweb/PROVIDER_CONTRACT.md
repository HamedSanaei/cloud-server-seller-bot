# LeaseWeb provider contracts (sellable EU provider #2)

Status: **implemented** — this repository contains TWO Leaseweb adapters
with different contracts:

| Adapter | Module | Port | Product |
| --- | --- | --- | --- |
| Leaseweb Public Cloud | `src/cloud_platform/providers/leaseweb/client.py` | `CloudProvider` (synchronous create) | hourly on-demand instances (`/cloud/v2/instances`) |
| Leaseweb VPS Ordering | `src/cloud_platform/providers/leaseweb/ordering.py` | `CloudProvider` + `OrderingProvider` | monthly VPS via the Ordering API (`/ordering/v1/products/vps`, `/account/v1/orders`) |

The **VPS Ordering adapter is the LEASEWEB-MVP path**. The Public Cloud
adapter is kept for the hourly contract and is described briefly in §7.

## 1. Transport and authentication (both adapters)

- Base URL (production): `https://api.leaseweb.com` (overridable via
  `LEASEWEB_API_BASE_URL`).
- Auth: `X-LSW-Auth: <API-KEY>` header — **plain key, no `Bearer` prefix**.
- The key lives in encrypted secret storage / `CredentialHolder`, never in
  logs, metric labels or audit payloads (only `key_hint`).

## 2. VPS Ordering API endpoints

| Operation | Endpoint |
| --- | --- |
| list VPS products (per location) | `GET /ordering/v1/products/vps?location=&limit=&offset=` |
| product detail (OS/config options, per-term prices) | `GET /ordering/v1/products/vps/{vpsId}?location=&contractTerm=&billingCycle=` |
| **place a VPS order (billable)** | `POST /ordering/v1/products/vps/{vpsId}/order` → `201 {orderId}` |
| list account orders | `GET /account/v1/orders?limit=&offset=` |
| inspect one order | `GET /account/v1/orders/{Id}` |
| provisioned VPS list / detail / power | `GET /publicCloud/v1/vps`, `GET /publicCloud/v1/vps/{id}`, `POST .../start|stop|reboot` |

The order body is `{location, operatingSystem, contractTerm, billingCycle}`
(plus optional controlPanel/diskUpgrade/serviceLevelAgreement). The 201
response is `{orderId}` — the order id is **never** treated as the final
server id; reconciliation resolves the provisioned VPS only via the order's
`equipmentId` (see §5). A datacenter+pack+startedAt match against the VPS
list is diagnostic evidence only and is **never** used to attach a VPS.

## 3. What the Orders API exposes — and what it does NOT expose

Verified against the official OpenAPI specs (`github.com/Leaseweb/api-definitions`,
`orders/` and `ordering/`).

**Exposes** (order + service schema):

- order `id`, `type` (`NEW_ORDER`), `createdAt`, `origin`, `quotation`,
  `contractId`
- per service: `productId` (family only, e.g. `VIRTUAL_SERVER`), `status`
  (`NEW_CONTRACT`/`SCHEDULED`/`TO_BE_PROVISIONED`/`ACTIVE`/`CANCELLED`/…),
  `deliveryEstimate`, `equipmentId`, `pricePerFrequency`, `currency`,
  `contractTerm`, `billingCycle`

**Does NOT expose**:

- the exact ordering VPS product id (e.g. `VPS02_1`) — only the
  `VIRTUAL_SERVER` family,
- the location/datacenter,
- the operating system / configuration,
- any client reference, correlation identifier or idempotency key attached
  to the order.

Consequence: **two independent customers ordering the same plan at the same
location with the same OS and the same provider price produce orders that
the Orders API cannot tell apart.** This fact drives the safety model in §4
and §5.

## 4. Ordering safety model (release hardening)

The Ordering API has no `Idempotency-Key` header and the order body carries
no client reference (verified, §3). Mathematically exactly-once provider
ordering is therefore **impossible**. The platform guarantees instead:

1. **Exactly-once local wallet effects** — one hold per checkout, one
   idempotent capture (unique ledger key), one release on definitive
   rejection.
2. **Exactly-once local operation identity** — checkout persists the server
   row, hold, `provider_orders` row and the `operations` row (deterministic
   key `order-create:{server_id}`) in ONE transaction before any provider
   call; the worker claims the operation atomically (`PENDING -> IN_FLIGHT`)
   so two workers can never POST the same order.
3. **At-most-one automatic POST per operation** — a fresh claimed operation
   POSTs exactly once; an operation with an ambiguous outcome is **never**
   automatically re-POSTed.
4. **No account-wide similarity dedup (removed)** — `place_order` does NOT
   scan recent account orders and does NOT suppress or reuse an order based
   on VIRTUAL_SERVER + price + currency + term + cycle similarity. It is
   completely valid for two customers to buy the exact same plan at the same
   time and receive two independent POSTs and two distinct provider order
   ids. The local operation ledger is the only dedup mechanism.

### POST outcome classification

| Outcome | Classification | Platform action |
| --- | --- | --- |
| `201` with `orderId` | accepted | persist order id FIRST, capture hold exactly once, reconcile |
| definitive 4xx rejection (400/401/403/404/409/422/423) | rejected | no order created; operation FAILED, hold released exactly once |
| connect refused / connect timeout / pool timeout | **provably not transmitted** | retryable: operation re-queued with the SAME key, a fresh POST of the same identity |
| read/write timeout, dropped connection, 5xx, **mutating 429**, missing/unparseable `orderId` | **ambiguous** | `PROVIDER_OUTCOME_UNKNOWN`; hold stays reserved; NO automatic second POST |

Note on 429: the official Leaseweb API standards do **not** state that a 429
guarantees the request was not processed. A mutating 429 is therefore
classified **conservatively as an unknown outcome**, never as a safe-to-retry
rejection. (Read-only 429s remain plain `ProviderRateLimited` and are
harmless to retry.)

## 5. Read-only recovery of ambiguous POSTs

`OrderingProvider.recover_order` runs a READ-ONLY `GET /account/v1/orders`
scan keyed on **provider-side facts only** — provider price snapshot
(`pricePerFrequency`, 1-cent tolerance), currency, contract term, billing
cycle, creation window. The **customer selling price is never used** for
provider correlation (a reseller margin must not break recovery).

Because the Orders API cannot prove ownership (§3), **generic similarity is
considered INSUFFICIENT**:

- zero candidates → `NO_MATCH` — absence is NOT provable (the order may be
  invisible yet); escalate to manual review.
- **one candidate → `AMBIGUOUS`** — an unrelated same-price/same-spec order
  from another customer satisfies the same facts; the platform NEVER
  auto-attaches a single generic candidate. (This replaced the previous
  `MATCHED`-on-one-candidate behavior, which could attach customer A's order
  to customer B's operation.)
- several candidates → `AMBIGUOUS`.
- scan failure → `SCAN_FAILED` — bounded retries, then manual review.

An `equipmentId`-bearing candidate is still not auto-attached for the
*account-order* recovery scan: the equipment chain proves the order's *spec*
(another identical order has the same attributes), not its *ownership* by
the local operation. The Leaseweb VPS Ordering adapter **never returns
`MATCHED`** from account-order recovery; `MATCHED` remains part of the port
contract only for providers that can prove identity (a client reference or
true idempotency key).

## 6. Provisioned-VPS correlation (equipmentId-only)

Activation requires a provider-supported identity. The only automatic
correlation the adapter performs is:

1. the exact provider order (`GET /account/v1/orders/{Id}`) reports a
   `service.equipmentId`, and
2. `GET /publicCloud/v1/vps/{equipmentId}` resolves that exact VPS.

Behavior while the identity is not yet usable:

- ACTIVE order + `equipmentId` present + VPS exists → attach / activate.
- ACTIVE order + `equipmentId` absent → `STILL_PROVISIONING`, keep polling.
- `equipmentId` present but the VPS endpoint returns 404 temporarily →
  `STILL_PROVISIONING`, keep polling — **no heuristic fallback attachment**.
- bounded grace period expires without a usable `equipmentId` → `NEEDS_REVIEW`
  (never a guessed resource id).

The VPS-list scan (datacenter + pack + startedAt) exists only for operator
diagnostics (`orders inspect` output) and **can never produce an automatic
`provider_server_id`** — an unrelated customer's single recently-started VPS
in the same location/pack satisfies the same facts.

Manual resource resolution: `orders resolve-vps <order_id> <vps_id>`
(read-only toward Leaseweb, audit-trailed, requires `--yes` and a reason).
It validates the VPS exists, then attaches it under the same settlement
gate (no delivery until the hold is captured and the CHARGE ledger entry
exists).

Manual review is the accepted cost for the rare ambiguous POST: incorrectly
attaching another customer's order (and capturing the wrong wallet, or
double-provisioning) is worse than a human verifying at the portal. The
operator runbook (`docs/operations/RUNBOOK.md`) documents the review flow,
including the `APIGW-CORRELATION-ID` response header that can be quoted to
Leaseweb support when asking whether a specific request created an order.

## 7. Payment settlement barrier

A provider order being accepted and a customer charge being settled are two
different facts:

1. **Persist provider acceptance first** — the `provider_order_id` is saved
   before any money movement and is never lost, and the provider POST is
   never repeated.
2. **Then settle locally** — an idempotent hold capture (deterministic
   ledger key `capture-{operation_key}`). Only after the hold is CAPTURED
   *and* the matching CHARGE ledger entry exists may the server enter
   PROVISIONING and delivery occur.

Local settlement outcomes (`ensure_order_payment_settled`):

- `SETTLED` — hold CAPTURED + exactly one CHARGE entry; activation may
  proceed.
- `RETRY_LATER` — transient capture failure; the provider order id stays
  attached, the hold stays reserved, **zero provider POSTs**, and the
  periodic reconciler retries the LOCAL capture only (bounded attempts,
  then `NEEDS_REVIEW`).
- `NEEDS_REVIEW` — e.g. hold RELEASED or missing after provider acceptance,
  or the bounded retry budget exhausted. No delivery, no provider POST; a
  financial review audit event is written.

Repair semantics:

- If the hold was debited (CAPTURED) but the CHARGE ledger insert failed,
  re-running the idempotent capture posts the missing CHARGE **without a
  second wallet debit** — the wallet is debited exactly once and exactly
  one CHARGE entry exists.
- Capture failure is **never** treated as provider failure: the order is
  not marked FAILED, the hold is not released, the POST is not retried, and
  no second operation key is created.
- Crash windows (provider-order-id saved but operation-complete save
  failed, or capture failed after persistence) are repairable by the same
  local settlement path on the next worker/reconciler pass.

Manual `orders resolve-existing` and `orders resolve-vps` use the same
settlement gate and never print "fully resolved / captured" when the
capture actually failed.

## 8. Error mapping (VPS Ordering adapter)

| HTTP | Platform error |
| --- | --- |
| 401/403 | `ProviderAuthError` |
| 404 | `ProviderNotFound` |
| 409/423 or "already" in message | `ProviderConflict` (definitive rejection for a POST) |
| 429 (read-only) | `ProviderRateLimited` (honors `Retry-After`, bounded retries) |
| 429 (mutating POST) | `ProviderOutcomeUnknown` (conservative, §4) |
| 5xx (read-only) | `ProviderUnavailable` |
| 5xx (mutating POST) | `ProviderOutcomeUnknown` |
| transport error that proves non-transmission (connect refused/timeout, pool timeout) | `ProviderUnavailable` (retryable) |
| any other transport error after transmission (read/write timeout, dropped connection) on a POST | `ProviderOutcomeUnknown` |

## 9. Public Cloud adapter (hourly, non-MVP)

- `GET/POST /cloud/v2/instances`, `GET/DELETE /cloud/v2/instances/{id}`,
  power/rebuild/network endpoints; capability set `LEASEWEB_CAPABILITIES`.
- Prices are ingested as `Decimal` major units via `PricingIngestionService`
  — never float, never hard-coded.
- The spec documents no `Idempotency-Key` header, so synchronous create is
  guarded by a get-before-create on the deterministic server name (the name
  IS a client-chosen identifier, unlike the Orders API) — same pattern as
  ArvanCloud; delete treats 404 as success. This is a different product and
  a different correlation mechanism; it does not apply to VPS ordering.

## 10. Selling checklist (operator, LEASEWEB-MVP)

1. Create a Leaseweb API key in the provider panel (invoice payment with no
   prepayment obligation is required for Ordering; otherwise the API returns
   a clear eligibility error — see `leaseweb doctor`).
2. Set `LEASEWEB_API_KEY` (and optionally `LEASEWEB_API_BASE_URL`,
   `LEASEWEB_LOCATIONS`) in `.env`.
3. `uv run alembic upgrade head`
4. `uv run python -m cloud_platform.cli leaseweb doctor` (read-only).
5. Sync the catalog / refresh offer prices: `uv run python -m cloud_platform.cli leaseweb sync-offers`.
6. Enable the offers you want to sell and set an explicit customer selling
   price (admin command / CLI) — a product is sellable ONLY when Leaseweb
   reports it, it is enabled, and it has an explicit sell price.
7. Order from Telegram (`خرید سرور`). There is intentionally NO CLI command
   that can place a billable order — every POST flows through the durable
   checkout → worker pipeline; the first real order follows the controlled
   procedure in `docs/operations/RUNBOOK.md`.
