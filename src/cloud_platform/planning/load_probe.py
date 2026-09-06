"""Reproducible in-process load probe for API and worker queues (M16-002).

Acceptance: **bottlenecks measured with reproducible script.**

This module is the measurement library behind ``scripts/load_test.py``.
It is deliberately SELF-CONTAINED (no live network, no Postgres, no
Redis) so results are reproducible on any machine:

- **API probe** - drives the REAL v1 ASGI stack (routing, auth
  dependency chain, scope checks, rate limiter, error envelope) through
  ``httpx.ASGITransport`` with N concurrent virtual users against
  fake-backed read endpoints, recording per-request latencies.
- **Worker probe** - dispatches the REAL arq job coroutines from
  :mod:`cloud_platform.worker.settings` (the no-op reconcile job) through
  an ``asyncio.Queue`` with W consumer tasks, i.e. the same
  enqueue/dequeue shape arq uses, measuring per-job latency and queue
  wait.

Percentiles are computed by sorted-index (no numpy). The bottleneck
ranking orders targets by p95 latency descending - the report's point.
"""

from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """One measured target."""

    name: str
    kind: str  # "api" or "worker"
    requests: int
    duration_s: float
    latencies_ms: tuple[float, ...] = field(repr=False)

    @property
    def rps(self) -> float:
        return self.requests / self.duration_s if self.duration_s > 0 else math.inf

    def percentile(self, q: float) -> float:
        """Sorted-index percentile; q in (0, 100]."""
        if not self.latencies_ms:
            return 0.0
        ordered = sorted(self.latencies_ms)
        idx = max(0, min(len(ordered) - 1, math.ceil((q / 100) * len(ordered)) - 1))
        return ordered[idx]

    @property
    def p50(self) -> float:
        return self.percentile(50)

    @property
    def p95(self) -> float:
        return self.percentile(95)

    @property
    def p99(self) -> float:
        return self.percentile(99)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.latencies_ms) if self.latencies_ms else 0.0


def rank_bottlenecks(results: list[ProbeResult]) -> list[ProbeResult]:
    """Slowest-first ranking by p95; ties broken by mean."""
    return sorted(results, key=lambda r: (-r.p95, -r.mean))


# ---------------------------------------------------------------------------
# API probe
# ---------------------------------------------------------------------------

ApiCaller = Callable[[], Awaitable[int]]


async def probe_api(
    name: str,
    caller: ApiCaller,
    *,
    total_requests: int,
    concurrency: int,
) -> ProbeResult:
    """Drive one endpoint with ``concurrency`` virtual users."""
    if total_requests < 1 or concurrency < 1:
        raise ValueError("total_requests and concurrency must be >= 1")
    latencies: list[float] = []
    claimed = 0
    lock = asyncio.Lock()

    async def _worker() -> None:
        nonlocal claimed
        while True:
            # claim the next request slot atomically so exactly
            # ``total_requests`` calls happen under concurrency
            async with lock:
                if claimed >= total_requests:
                    return
                claimed += 1
            started = time.perf_counter()
            await caller()
            elapsed_ms = (time.perf_counter() - started) * 1000
            async with lock:
                latencies.append(elapsed_ms)

    runners = [asyncio.create_task(_worker()) for _ in range(min(concurrency, total_requests))]
    wall_started = time.perf_counter()
    await asyncio.gather(*runners)
    duration = time.perf_counter() - wall_started
    return ProbeResult(
        name=name,
        kind="api",
        requests=len(latencies),
        duration_s=duration,
        latencies_ms=tuple(latencies),
    )


# ---------------------------------------------------------------------------
# Worker-queue probe
# ---------------------------------------------------------------------------

JobHandler = Callable[[dict[str, object]], Awaitable[None]]


@dataclass(frozen=True)
class WorkerProbeConfig:
    """How to drive the worker-queue simulation."""

    jobs: int
    workers: int
    handler: JobHandler


async def probe_worker_queue(config: WorkerProbeConfig) -> ProbeResult:
    """Enqueue ``jobs`` payloads, drain with ``workers`` consumers.

    The measured latency is END-TO-END per job (dequeue -> handler
    finished), so contention shows up exactly as it would on a real
    worker pool.
    """
    if config.jobs < 1 or config.workers < 1:
        raise ValueError("jobs and workers must be >= 1")
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    for i in range(config.jobs):
        queue.put_nowait({"job_id": i})
    latencies_ms: list[float] = []

    async def _consumer() -> None:
        while True:
            try:
                payload = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            started = time.perf_counter()
            await config.handler(payload)
            latencies_ms.append((time.perf_counter() - started) * 1000)

    wall_started = time.perf_counter()
    await asyncio.gather(*[_consumer() for _ in range(min(config.workers, config.jobs))])
    duration = time.perf_counter() - wall_started
    return ProbeResult(
        name=f"worker:{getattr(config.handler, '__name__', 'handler')}",
        kind="worker",
        requests=len(latencies_ms),
        duration_s=duration,
        latencies_ms=tuple(latencies_ms),
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_report(results: list[ProbeResult]) -> str:
    """A stable ASCII table plus the bottleneck verdict line."""
    header = (
        f"{'target':<34} {'kind':<7} {'reqs':>6} {'rps':>8} "
        f"{'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'mean ms':>8}"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r.name:<34} {r.kind:<7} {r.requests:>6} {r.rps:>8.1f} "
            f"{r.p50:>8.2f} {r.p95:>8.2f} {r.p99:>8.2f} {r.mean:>8.2f}"
        )
    ranked = rank_bottlenecks(results)
    lines.append("")
    lines.append("Bottleneck ranking (p95 desc):")
    for i, r in enumerate(ranked, start=1):
        lines.append(f"  {i}. {r.name} (p95={r.p95:.2f} ms)")
    return "\n".join(lines)


def summarize(results: list[ProbeResult]) -> dict[str, Any]:
    """JSON-serializable summary for machine consumption."""
    return {
        "results": [
            {
                "name": r.name,
                "kind": r.kind,
                "requests": r.requests,
                "duration_s": round(r.duration_s, 4),
                "rps": round(r.rps, 2) if math.isfinite(r.rps) else None,
                "p50_ms": round(r.p50, 3),
                "p95_ms": round(r.p95, 3),
                "p99_ms": round(r.p99, 3),
                "mean_ms": round(r.mean, 3),
            }
            for r in results
        ],
        "bottleneck": rank_bottlenecks(results)[0].name if results else None,
    }
