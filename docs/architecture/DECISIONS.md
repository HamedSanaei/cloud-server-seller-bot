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
**Status:** accepted. A provider account represents one credential/capacity/rate-limit boundary, enabling future Hetzner project pools and other vendors.

## ADR-010 — No floats for money
**Status:** accepted. Use Decimal/integer minor units plus explicit currency.

## ADR-011 — Frozen dependency direction
**Status:** accepted. Domain modules import only the standard library and provider-neutral
`cloud_platform.core` utilities. Provider adapters, database/ORM code, aiogram and FastAPI are
import-forbidden in `cloud_platform.modules`; adapters may depend inward, never the reverse.
