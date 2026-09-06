# ADR-013: Provider-worker extraction — deferred (M16-007)

**Status:** accepted (defer extraction; keep provider workers in the monolith).
**Date:** 2026-09-04. **Decides:** M16-007.

## Context

M16-002 measured the system with the reproducible in-process probe
(`scripts/load_test.py`, `docs/ops/load_results.json`, 2026-08-27):

- Worker dispatch p95 = 0.01 ms at 500 jobs / 4-pool — dispatch overhead is
  negligible; provider I/O (HTTP to Hetzner/LeaseWeb/ArvanCloud), not CPU,
  dominates worker time.
- The API bottleneck is `GET /v1/servers` serialization (p95 31.7 ms),
  unrelated to provider I/O.

Provider work (provisioning M07-002, deletes M07-007, reconcilers M07-003/
M07-005/M07-008, sync jobs M04-006) is I/O-bound with bounded concurrency
(per-account limit M07-011, client-side throttles, 429 backoff M04-009) and
shares the operation ledger, server rows, holds and audit trail with the
API processes in one Postgres database.

## Decision

**Do not extract a standalone provider-worker service.** A separate deploy
would add network hops, duplicate credential handling and split-brain
scheduling risk, while measured data shows I/O waits — not process
capacity — are the limit. Isolation is already achieved cheaper via
M16-004 partitioned arq queues (provisioning / billing / notify) that can
run as separate processes from the same image (`worker-provisioning`,
`worker-billing`, `worker-notify` roles) without any code split.

## Consequences

- Scale provider work by running more worker processes / queue roles;
  keep one image, one migration chain, one credential-rotation path.
- The per-account concurrency limit (M07-011) and advisory locks remain the
  cross-process safety mechanism — no distributed lock service needed.
- Revisit when any trigger holds for 2 consecutive weekly probes:
  1. provider p95 wait times force queue ages past the reconcile SLOs;
  2. provider traffic needs network-level isolation (egress policy/VPC);
  3. provider SDK dependencies conflict with the API image.
