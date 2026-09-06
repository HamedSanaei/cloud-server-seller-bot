# Leaseweb Monthly VPS MVP — implementation decisions

Status: **implemented (LEASEWEB-MVP track).** This note records the audit
findings and the decisions taken to ship the smallest production-usable
Leaseweb monthly-VPS reseller, per the LEASEWEB-MVP brief.

## 1. Audit summary (Phase 1)

Reusable as-is:

- Wallet / ledger / hold infrastructure (`modules/wallet`) — atomic holds,
  `SELECT FOR UPDATE` concurrency safety, append-only ledger with unique
  `(wallet_id, idempotency_key)`, idempotent capture/release. **Used unchanged.**
- Operation ledger (`modules/operations`) — durable provider-mutation intents
  with atomic claims and deterministic keys. **Used unchanged** (new op type
  added).
- RBAC + audit trail (`modules/users`, `modules/audit`) — admin wallet
  adjustments and offer changes reuse `Permission.WALLET_ADJUST` /
  `Permission.ADMIN_MANAGE_SETTINGS` with mandatory reasons and audit rows.
- Telegram shell (`bot/ui.py`, signed M08-001 callbacks) and the Persian
  message catalog (`core/i18n.py`).
- `CloudProvider` port + `ProviderRegistry` + error hierarchy + retry policy.

Not reusable without a provider-neutral change:

- The existing `LeaseWebProvider` targets **Public Cloud instances
  (`/publicCloud/v1/instances`)** — usage-billed, synchronous-ish launch.
  The MVP sells **ordering VPS (`/ordering/v1`)** — fixed monthly products,
  ordered asynchronously via `orderId`. It is a different product family, so
  a second adapter was added (the Public Cloud adapter stays for Hetzner-style
  hourly flows and its tests stay green).
- `CloudProvider.create_server()` returns a ready `ProviderServer`; it cannot
  represent "we got an order id, the server comes later". A minimal optional
  port, `OrderingProvider` (probe: `ordering_support_of()`), was added — the
  same optional-capability pattern as `PowerEffectProbe`/`rebuild_support_of`.
  Hetzner is untouched and stays compatible (it simply does not implement the
  ordering port).
- The hourly price book / margin rules derive the selling price from provider
  cost. The brief demands **explicit admin-configured customer prices**, so a
  new `sellable_offers` table stores provider cost and customer selling price
  as **separate snapshots** (both integer minor units, never float).
- Hourly accrual (`billing.AccrualJob`) and the low-balance auto-delete policy
  must **never touch prepaid monthly servers** — they are gated on the new
  `servers.billing_model` column (`hourly` vs `prepaid_monthly_fixed`).

## 2. Leaseweb Ordering API (verified against the official spec, Aug 2026)

Source: `github.com/Leaseweb/api-definitions` (`ordering/`, `orders/`, `vps/`
OpenAPI specs) + kb.leaseweb.com. Auth: `X-LSW-Auth: <API-KEY>` on
`https://api.leaseweb.com`. Ordering is only available to verified customers
with invoice/post-payment enabled (per entity/location).

| operation | endpoint | notes |
| --- | --- | --- |
| list VPS products | `GET /ordering/v1/products/vps?location=&limit=&offset=` | `vpss[]` with `id, name, vCpu, vRam, nvmeStorage, traffic, price{currency, basePrice, discount, total}` |
| product detail | `GET /ordering/v1/products/vps/{vpsId}?location=` (+ `contractTerm`, `billingCycle`, …) | `configurationOptions{operatingSystem[], controlPanel[], diskUpgrade[], serviceLevelAgreement[]}` each `{name, selected, price, currency}`; `price{contractTerms[], billingCycles[], total, ...}` |
| place order | `POST /ordering/v1/products/vps/{vpsId}/order` | body `{location (required), operatingSystem?, controlPanel?, diskUpgrade?, serviceLevelAgreement?, contractTerm?, billingCycle?}` → **201 `{orderId: int}`** |
| inspect order | `GET /account/v1/orders/{Id}` | `services[] {id, productId, status (NEW_CONTRACT/ACTIVE/SCHEDULED/…), deliveryEstimate, equipmentId, pricePerFrequency, currency, contractTerm, billingCycle}` |
| list VPS | `GET /publicCloud/v1/vps?datacenter/…` | `vps[] {id, pack, datacenter, image, state (RUNNING/STARTING/STOPPED/STOPPING), ips[], startedAt, …}` |
| VPS detail | `GET /publicCloud/v1/vps/{vpsId}` | adds `contract{id, type, term, billingFrequency, state, startsAt, endsAt, sla, controlPanel, dataTraffic}` + `resources{cpu, memory}` |
| power | `POST /publicCloud/v1/vps/{vpsId}/start\|stop\|reboot` | |
| IPs | `GET /publicCloud/v1/vps/{vpsId}/ips` | |

Decisions:

- MVP order terms: `contractTerm=1_MONTH`, `billingCycle=1_MONTH`
  (configurable via env). The monthly provider price is read from
  `price.contractTerms[1_MONTH].total` (fallback `billingCycles[1_MONTH].total`,
  then `price.total`), parsed with `Decimal` from strings — never float.
- OS selection: only `operatingSystem` options with `price == 0` are exposed
  by default (`LEASEWEB_ORDER_OS_ONLY_FREE=true`); paid OS/control
  panels/SLA/disk upgrades are out of MVP scope so the displayed monthly price
  is always exact. Optional `LEASEWEB_OS_ALLOWLIST` narrows further.
- Location display metadata (country/city) is a small operator-owned map in
  the syncer (geography, not price); `LEASEWEB_LOCATIONS` (default
  `AMS-01,FRA-01`) is the sync allowlist. A product is sellable only when it
  is **reported by Leaseweb for that location AND enabled by admin AND has an
  explicit selling price** — the `sellable_offers` row is the single gate.

## 3. Idempotency for chargeable POSTs

The Ordering API has no `Idempotency-Key` header and the order body has no
reference field (verified against the official spec), so **provider-side
exactly-once ordering is impossible**. The platform guarantees exactly-once
LOCAL wallet effects, exactly-once LOCAL operation identity, at-most-one
automatic POST per operation, and reconciliation/manual review before any
second chargeable POST:

1. Checkout persists, in ONE transaction, the `REQUESTED` server row (unique
   `idempotency_key` = signed bot-callback key), the wallet hold (unique
   `(wallet_id, key)`), the `provider_orders` row (unique `server_id`), and
   the `operations` row with the deterministic key `order-create:{server_id}`.
   **All before any provider call.**
2. The worker claims the operation atomically (`PENDING -> IN_FLIGHT`), so two
   workers can never POST the same order. The provider order id is persisted
   before the hold is captured.
3. A fresh claimed operation **always POSTs exactly once** — there is **no
   get-before-create scan** and no account-wide similarity dedup. Recent
   orders are never used to suppress or reuse an order: the Orders API cannot
   distinguish two independent customers buying the same plan at the same
   price (it exposes no exact product id, location, OS or client reference),
   so two same-plan checkouts produce two independent POSTs and two distinct
   provider order ids.
4. A worker crash around the POST / an ambiguous outcome leaves the
   operation `OUTCOME_UNKNOWN` — it is **never** re-POSTed automatically. A
   READ-ONLY recovery scan (`GET /account/v1/orders`, provider facts only)
   counts candidates; because generic similarity cannot prove identity, ANY
   candidate count (0, 1 or many) escalates to `needs_review` for a human.
   The operator verifies at the portal and may then reopen the operation
   (same key) via `orders retry`.
5. Reconcile-vs-place separation: the reconciler **never POSTs** — it only
   inspects orders and VPSes. Only the claimed worker path mutates.

## 4. Checkout money flow (financially safe)

1. User selects enabled offer + OS (server-side validated against the live
   product API, price-0 options only).
2. Offer reloaded from DB; selling price read from the offer row (explicit
   admin price).
3. Wallet hold: `leaseweb-order:{idempotency_key}` (atomic, race-free).
4. Idempotency replay returns the original server/hold/order — repeated
   Telegram callbacks can never double-charge or double-order.
5. Worker POSTs the order; on **201 (accepted)** the hold is captured exactly
   once (`HoldService.capture_hold` is idempotent; ledger key
   `capture-leaseweb-order:{ik}` is unique).
6. Definitive provider rejection (4xx before acceptance) → order FAILED,
   server ERROR, hold released.
7. Ambiguous (read/write timeout after transmission, dropped connection,
   5xx, mutating 429 — a 429 does NOT provably mean the order was not
   processed) → order/operation `OUTCOME_UNKNOWN`; the hold stays reserved;
   NO automatic second POST. Only provably pre-transmission failures
   (connect refused/timeout, pool timeout) re-queue with the SAME key.
8. Reconciliation finds the provisioned VPS (order service `equipmentId`, else
   VPS list matched by datacenter+pack+startedAt window; ambiguity →
   `needs_review`) → server RUNNING with provider id + IPs; renewal record
   created from `contract.endsAt`; the user is notified with connection
   details.

## 5. Renewals (MVP safeguards)

- `renewals` table: 1:1 per server — provider contract id, order reference,
  purchased_at, provider renewal date (from `contract.endsAt`, estimated
  fallback), customer renewal price, status, auto-charge flag.
- Daily `check_renewals` job: notify at 7/3/1 days (deduplicated per
  (server, level, period)); at 3 and 1 days flag insufficient balance; on the
  renewal date, if balance covers the price and auto-charge is on, post
  **exactly one** monthly debit under `renewal:{server_id}:{period}`; advance
  the renewal date by one month. Insufficient → `INSUFFICIENT_FUNDS` +
  `MANUAL_CANCELLATION_REQUIRED` + admin alert. Repeated runs are no-ops
  (ledger idempotency + unique notification rows).
- Cancellation: the current VPS API has **no verified cancel/delete endpoint**
  (verified in the OpenAPI spec: only start/stop/reboot/reinstall/snapshot/
  ips/credentials). We do NOT invent one — services in need of cancellation
  are flagged `MANUAL_CANCELLATION_REQUIRED`, admins are alerted with
  provider order/contract ids, and `docs/operations/RUNBOOK.md` documents the
  exact portal procedure.

## 6. Safety rule for live orders

- `LEASEWEB_ALLOW_LIVE_ORDER_TEST=false` (default). The CLI
  `leaseweb smoke-order` (the only verification path that POSTs a real order)
  refuses to run unless it is explicitly `true`. Production order placement
  (worker, real customer purchases) is the product and is not gated by the
  flag; automated tests always mock HTTP and never create billable VPSes.

## 7. Files (MVP delta)

See `docs/roadmap/TASKS.yaml` (LEASEWEB-MVP track) and the final handoff in
the session report. Key modules: `providers/leaseweb/ordering.py`,
`providers/leaseweb/ordering_sync.py`, `modules/offers/`,
`modules/orders/`, `modules/renewals/`, `modules/checkout/`,
`bot/notifier.py`, `cli/`, migrations `0030_leaseweb_mvp.py`.
