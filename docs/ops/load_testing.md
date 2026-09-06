# Load testing runbook (M16-002)

Reproduce the platform's API and worker-queue bottleneck measurements:

```bash
uv run python scripts/load_test.py --requests 200 --users 8 \
    --worker-jobs 500 --worker-pool 4 --json docs/ops/load_results.json
```

## What it measures

| Target | Pipeline exercised |
| --- | --- |
| `GET /health/live` | routing only (anonymous) |
| `GET /v1/catalog/offers` | bearer-auth chain + scope check + rate limiter + catalog read port |
| `GET /v1/servers` | auth chain + ownership-scoped list serialization (20 rows) |
| `GET /v1/ssh-keys` | auth chain + empty-list service round-trip |
| `GET /v1/wallet unauth` | the one error envelope fast-fail path (401) |
| worker queue | real arq job coroutines drained through an asyncio queue pool |

Everything runs in-process over `httpx.ASGITransport`; Postgres, Redis
and live providers are replaced by in-memory fakes so numbers are
reproducible on any machine. They measure FRAMEWORK and APPLICATION
overhead - not database latency.

## Baseline (2026-08-27)

The committed `docs/ops/load_results.json` is the current baseline for
the flags above: `--requests 200 --users 8 --worker-jobs 500 --worker-pool 4`.
Bottleneck verdict: `GET /v1/servers` (p95 ~31.7 ms) leads the ownership-
scoped list serialization; the worker dispatch path sits at ~0.01 ms p95
(no I/O - pure overhead), orders of magnitude below any real job.
Re-run the command above to refresh the baseline and diff p95s.

## Reading the output

- Percentiles are per-request latencies; the **bottleneck ranking**
  orders targets by p95 descending.
- The current bottleneck is the ownership-scoped server list: building
  the page costs more than the auth chain. That is the expected shape -
  auth must stay cheap, view assembly may grow with features.
- The worker probe measures end-to-end job latency (dequeue to handler
  completion); at zero I/O it quantifies pure dispatch overhead, which
  should stay far below any real job's DB work.

Interpretation rules:

1. Compare runs with identical flags only (`--requests`, `--users`).
2. A regression is a p95 increase beyond run-to-run noise (~2x on this
   machine class), not a p50 wiggle.
3. Never "fix" a bottleneck by weakening auth or ownership checks;
   optimize behind the same contract.
