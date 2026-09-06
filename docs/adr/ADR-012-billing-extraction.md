# ADR-012: Billing service extraction — NOT YET (M16-006)

Status: **decided** — keep billing in the modular monolith; re-evaluate at
10x load.

## Context

Load model (`scripts/load_test.py`, M16-001/002): the accrual job settles
complete quantum periods under a Postgres advisory lock; the ledger is
append-only with a unique idempotency key; billing reads immutable price
snapshots (never the live catalog).

## Measured coupling / load

- Billing shares one transaction/session with wallet holds, ledger posting,
  accrual business records and audit events. Extraction would turn ~6
  same-process repository calls into distributed transactions (outbox +
  saga) with no measured need.
- Queue partitioning (M16-004) already isolates billing (`billing` queue)
  from provisioning; the M16-002 load probe shows the accrual pass is
  index-bound (M16-003), not CPU-bound.
- No hot-module evidence: billing p95 is dominated by Postgres I/O, which
  extraction would not improve (same database).

## Decision

Do NOT extract a billing service. Keep `modules/billing` + `modules/pricing`
+ `modules/wallet` in-process; scale via more `billing`-queue workers and
read replicas if needed.

## Consequences

- No new service, schema, or deployment topology.
- Revisit when: accrual p95 > 60s for two consecutive releases, or ledger
  write throughput exceeds single-primary capacity.
