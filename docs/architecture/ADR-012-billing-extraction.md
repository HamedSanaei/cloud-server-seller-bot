# ADR-012: Billing service extraction — deferred (M16-006)

**Status:** accepted (defer extraction; keep billing in the modular monolith).
**Date:** 2026-09-04. **Decides:** M16-006.

## Context

M16-002 measured the system with the reproducible in-process probe
(`scripts/load_test.py`, `docs/ops/load_results.json`, 2026-08-27):

- `GET /health/live` p95 = 0.55 ms (auth chain baseline).
- `GET /v1/catalog/offers` p95 = 19.1 ms, `GET /v1/ssh-keys` p95 = 18.3 ms.
- `GET /v1/servers` p95 = 31.7 ms — the measured bottleneck, and it is
  **ownership-scoped list serialization**, not billing.
- Worker dispatch p95 = 0.01 ms — the queue layer is not saturated.

Billing paths (accrual M06-005, final charge M06-006, low-balance M06-007,
margin report M06-008) share one Postgres transaction with the wallet
ledger, holds, accrual-period records and the audit trail. Money movement
relies on database-level uniqueness (ledger idempotency keys, accrual
record keys) plus session advisory locks — i.e. the correctness argument
*is* the single-database transaction.

## Decision

**Do not extract billing into a service.** Splitting it now would replace
local transactions with distributed settlement (sagas/2PC) for the most
money-sensitive paths, while the measured load shows no billing-side
bottleneck to relieve.

## Consequences

- Keep `modules/billing`, `modules/pricing`, `modules/wallet` in-process;
  scale by adding worker processes behind the partitioned queues (M16-004).
- Revisit when any trigger holds for 2 consecutive weekly probes:
  1. accrual-job p95 exceeds the schedule interval (backlog grows);
  2. billing-owned tables need an independent failover/retention policy;
  3. a second team must deploy billing on its own cadence.
- Revisit input is already collected: `scripts/load_test.py` output plus
  `billing.*` audit counters and `accrual_periods` growth.
