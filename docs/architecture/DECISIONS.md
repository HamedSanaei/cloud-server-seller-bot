# Architecture decision log

## ADR-001 — Modular monolith before microservices
**Status:** accepted. One repository and database, three process types, explicit module boundaries. Extract only after production evidence.

## ADR-002 — Provider adapter boundary
**Status:** accepted. Domain/application code sees normalized provider ports. Hetzner SDK/API types stay inside the adapter.

## ADR-003 — PostgreSQL is the source of truth
**Status:** accepted. Redis may cache/queue but may not be the sole store for balances, resource ownership or billing facts.

## ADR-004 — Immutable wallet ledger
**Status:** accepted. Never "UPDATE balance = balance - x" as the accounting source of truth. Ledger entries are append-only and idempotent.

## ADR-005 — Asynchronous provisioning with reconciliation
**Status:** accepted. Telegram/API requests create intent quickly; workers perform provider mutations and reconcile ambiguous outcomes.

## ADR-006 — Transactional outbox
**Status:** accepted for persistence milestone. DB state + required async side-effect intent are committed atomically.

## ADR-007 — Separate provider cost from selling price
**Status:** accepted. Both become snapshots when a server/order is created. Historical billing is insulated from catalog changes.

## ADR-008 — Capability-driven UI
**Status:** accepted. Features such as rescue, snapshot, floating IP and rDNS are visible only when the provider/resource supports them.

## ADR-009 — Multiple provider accounts are first-class
**Status:** accepted. Stable credential IDs identify independent credential/Project
boundaries; multiple tokens do not prove independent owning-account quotas.
Hetzner new creates use active, product/location-qualified routes and complete
Project inventory. An optional operator-confirmed ceiling permits a known-full
Project to be skipped; no undocumented numeric provider limit is inferred.

Native price currency comes from that credential's official `/pricing`
envelope (`pricing.currency`), never an invented per-location field or EUR
default. Both billing families retain exact native rates and currency.

Before a billable POST, the existing operation JSON stores `create_routing`
(version, policy, original catalog account, ordered account attempts). One
transaction fences the claim generation and commits the fulfillment pin plus
`sent`; another binds accepted provider identity to that same account. Only an
adapter-proven documented capacity refusal permits another account. Unknown or
accepted outcomes require same-account read-only recovery, never another POST.
Generic operation saves cannot overwrite receipt-backed creates.

Returned operations retain the generation committed under the lock; they never
refresh into a subsequent recovery owner's generation. A committed definitive
non-capacity refusal can be fenced and finalized after a crash, not re-POSTed.
Monthly identity acceptance does not unlock server activation: the server stays
`requested` until the existing hold/ledger settlement barrier succeeds.

Catalog provenance and all accepted price/image/product/location/disk facts stay
immutable. Hourly fingerprint v3 separates that provenance from receipt-owned
fulfillment; v1/v2 contracts and historical attempts without proof remain pinned.
Monthly contracts retain their original provenance in the existing server JSON.
No new table or migration is required. Telegram terminal confirmations use a
shared-store, owner-bound nonce for the shown stable image/OS and selling price;
replays reach the same application idempotency key.

Historical direct-create recovery is selected by the optional recovery port
on the pinned adapter, not a provider-name branch. Missing owning credentials
never authorize another POST.

## ADR-010 — No floats for money
**Status:** accepted. Use Decimal/integer minor units plus explicit currency.

## ADR-011 — Frozen dependency direction
**Status:** accepted. Domain modules import only the standard library and provider-neutral
`cloud_platform.core` utilities. Provider adapters, database/ORM code, aiogram and FastAPI are
import-forbidden in `cloud_platform.modules`; adapters may depend inward, never the reverse.

## ADR-012 — Paid hourly coverage before delivery
**Status:** accepted. Reserve the frozen first-hour selling price before create;
capture it at provider readiness before delivery/power. Persist an exact
activation anchor and exclusive paid-through watermark. Buy each next hour
through an idempotent ledger fact under the PostgreSQL advisory lock, with a
durable ARQ renewal five seconds before the boundary. Insufficient early
funds preserve existing coverage; insufficient funds at expiry request the
reconciled delete saga. Stopped resources remain billable until deleted.

## ADR-013 — Checkout identity belongs to the application
**Status:** accepted. All server purchase services require an owner-verified
Iranian Telegram contact and checksum-valid national ID, not just a UI check.
Contact sender/account ownership, private-chat and no-forward constraints are
mandatory. This is not legal registry verification. Shared pending navigation
stores only stage and signed continuation, never identity values.

## ADR-014 — Payment attempts and administrator credits fail closed
**Status:** accepted. Gateway switches persist and affect new invoices only.
AtlasPay creation is a single durable local attempt, never an assumed remote
idempotent POST; ambiguous creation is read-only/manual recovery. Authoritative
settlement matches the frozen invoice facts and credits exactly once.
Administrator wallet adjustments atomically append wallet, immutable ledger,
signed keyed audit and business outbox facts. Changed replay facts fail closed;
historical unsigned adjustments without signed audit proof cannot be replayed.
