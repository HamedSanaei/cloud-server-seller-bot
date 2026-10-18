"""Low-cardinality catalog instrumentation and context-local SQL attribution.

CPU is process CPU DURING a scope, not exclusive coroutine CPU when jobs overlap.
SQL counters count cursor executions; commit/rollback are separate protocol events.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from prometheus_client import Counter, Gauge, Histogram
from sqlalchemy import event

from cloud_platform.observability.metrics import metrics

logger = logging.getLogger(__name__)
registry = metrics.registry
sql_statements = Counter(
    "cloud_platform_catalog_sql_total", "Catalog cursor executions", ("stage",), registry=registry
)
transactions = Counter(
    "cloud_platform_catalog_transactions_total",
    "Catalog transaction ends",
    ("stage", "outcome"),
    registry=registry,
)
stage_duration = Histogram(
    "cloud_platform_catalog_stage_seconds", "Catalog stage wall time", ("stage",), registry=registry
)
stage_cpu = Counter(
    "cloud_platform_catalog_process_cpu_seconds",
    "Inclusive process CPU during a stage; not exclusive task CPU",
    ("stage",),
    registry=registry,
)
active_stages = Gauge(
    "cloud_platform_catalog_active_stages", "Concurrent catalog stages", registry=registry
)
records = Counter(
    "cloud_platform_catalog_records_total",
    "Catalog record outcomes",
    ("kind", "outcome"),
    registry=registry,
)
http_attempts = Counter(
    "cloud_platform_provider_http_attempts_total",
    "Actual HTTP attempts including retries",
    ("provider", "operation", "outcome"),
    registry=registry,
)
http_duration = Histogram(
    "cloud_platform_provider_http_attempt_seconds",
    "Actual HTTP attempt latency",
    ("provider", "operation"),
    registry=registry,
)
probe_cache = Counter(
    "cloud_platform_catalog_probe_cache_total",
    "Discovery negative cache outcomes",
    ("outcome",),
    registry=registry,
)
discovery_active = Gauge(
    "cloud_platform_catalog_discovery_active",
    "Active discovery slots in this worker, including cache reads and retry waits",
    registry=registry,
)


@dataclass
class StageSample:
    name: str
    queries: int = 0
    commits: int = 0
    rollbacks: int = 0
    parent: StageSample | None = None


current_stage: ContextVar[StageSample | None] = ContextVar("catalog_stage", default=None)


@contextmanager
def stage(name: str) -> Iterator[StageSample]:
    sample = StageSample(name, parent=current_stage.get())
    token = current_stage.set(sample)
    wall, cpu = time.perf_counter(), time.process_time()
    active_stages.inc()
    try:
        yield sample
    finally:
        duration, cpu_delta = time.perf_counter() - wall, time.process_time() - cpu
        stage_duration.labels(name).observe(duration)
        stage_cpu.labels(name).inc(max(0, cpu_delta))
        active_stages.dec()
        current_stage.reset(token)
        logger.info(
            "catalog stage=%s wall_seconds=%.6f process_cpu_inclusive_seconds=%.6f "
            "sql=%d commits=%d rollbacks=%d",
            name,
            duration,
            cpu_delta,
            sample.queries,
            sample.commits,
            sample.rollbacks,
        )


def install_sql_instrumentation(engine: Any) -> None:
    """Install once per engine; one ContextVar lookup outside catalog work."""
    target = engine.sync_engine
    if event.contains(target, "before_cursor_execute", _before_cursor):
        return
    event.listen(target, "before_cursor_execute", _before_cursor)
    event.listen(target, "commit", _commit)
    event.listen(target, "rollback", _rollback)


def _before_cursor(*_args: Any) -> None:
    sample = current_stage.get()
    while sample is not None:
        sample.queries += 1
        sql_statements.labels(sample.name).inc()
        sample = sample.parent


def _commit(*_args: Any) -> None:
    sample = current_stage.get()
    while sample is not None:
        sample.commits += 1
        transactions.labels(sample.name, "commit").inc()
        sample = sample.parent


def _rollback(*_args: Any) -> None:
    sample = current_stage.get()
    while sample is not None:
        sample.rollbacks += 1
        transactions.labels(sample.name, "rollback").inc()
        sample = sample.parent
