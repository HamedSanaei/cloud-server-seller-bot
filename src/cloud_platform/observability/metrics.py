"""Prometheus metrics for API, jobs, provider calls and billing events (M11-001).

Each :class:`PlatformMetrics` instance owns a dedicated
:class:`~prometheus_client.CollectorRegistry`, so tests build isolated
registries without colliding with the process default. The module-level
:data:`metrics` singleton is the process-wide registry shared by the API
``/metrics`` endpoint and every instrumented service.

Cardinality discipline: label values are closed sets (route templates, job
names, provider + endpoint-shape operations, short outcome codes) — never
user ids, server ids or raw paths.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)

#: Buckets for request-like latency (API, provider calls).
REQUEST_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

#: Buckets for background job durations.
JOB_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 900.0)

#: Provider exception -> stable, closed outcome label (order matters: most
#: specific first).
_PROVIDER_OUTCOMES: tuple[tuple[type[ProviderError], str], ...] = (
    (ProviderAuthError, "auth_error"),
    (ProviderNotFound, "not_found"),
    (ProviderConflict, "conflict"),
    (ProviderRateLimited, "rate_limited"),
    (ProviderUnavailable, "unavailable"),
    (ProviderError, "provider_error"),
)


def provider_outcome(error: Exception) -> str:
    """A closed outcome label for a failed provider call."""
    for cls, label in _PROVIDER_OUTCOMES:
        if isinstance(error, cls):
            return label
    return "error"


class PlatformMetrics:
    """All platform metric families on one registry."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()

        # -- API ----------------------------------------------------------
        self.api_requests_total = Counter(
            "cloud_platform_api_requests_total",
            "API requests by method, route template and status",
            ("method", "path", "status"),
            registry=self.registry,
        )
        self.api_request_duration_seconds = Histogram(
            "cloud_platform_api_request_duration_seconds",
            "API request duration in seconds",
            ("method", "path"),
            buckets=REQUEST_BUCKETS,
            registry=self.registry,
        )

        # -- Jobs ----------------------------------------------------------
        self.job_runs_total = Counter(
            "cloud_platform_job_runs_total",
            "Background job runs by job and status",
            ("job", "status"),
            registry=self.registry,
        )
        self.job_duration_seconds = Histogram(
            "cloud_platform_job_duration_seconds",
            "Background job duration in seconds",
            ("job",),
            buckets=JOB_BUCKETS,
            registry=self.registry,
        )

        # -- Provider ------------------------------------------------------
        self.provider_calls_total = Counter(
            "cloud_platform_provider_calls_total",
            "Provider API calls by provider, operation and outcome",
            ("provider", "operation", "outcome"),
            registry=self.registry,
        )
        self.provider_call_duration_seconds = Histogram(
            "cloud_platform_provider_call_duration_seconds",
            "Provider API call duration in seconds",
            ("provider", "operation"),
            buckets=REQUEST_BUCKETS,
            registry=self.registry,
        )

        # -- Billing -------------------------------------------------------
        self.billing_events_total = Counter(
            "cloud_platform_billing_events_total",
            "Billing fund events (holds/charges) by event and result",
            ("event", "result"),
            registry=self.registry,
        )

    # -- instrumentation helpers ------------------------------------------

    @asynccontextmanager
    async def job(self, name: str) -> AsyncIterator[None]:
        """Time one background job run; status ok|error."""
        started = time.perf_counter()
        status = "ok"
        try:
            yield
        except Exception:
            status = "error"
            raise
        finally:
            self.job_runs_total.labels(job=name, status=status).inc()
            self.job_duration_seconds.labels(job=name).observe(time.perf_counter() - started)

    @asynccontextmanager
    async def provider_call(self, provider: str, operation: str) -> AsyncIterator[None]:
        """Time one provider API call; outcome success|<closed error code>."""
        started = time.perf_counter()
        outcome = "success"
        try:
            yield
        except Exception as exc:
            outcome = provider_outcome(exc)
            raise
        finally:
            self.provider_calls_total.labels(
                provider=provider, operation=operation, outcome=outcome
            ).inc()
            self.provider_call_duration_seconds.labels(
                provider=provider, operation=operation
            ).observe(time.perf_counter() - started)

    def record_billing_event(self, event: str, result: str) -> None:
        """Count one billing fund event (e.g. hold_created / hold_captured)."""
        self.billing_events_total.labels(event=event, result=result).inc()

    def render(self) -> bytes:
        """The Prometheus text exposition for the /metrics endpoint."""
        return generate_latest(self.registry)


#: The process-wide default metrics instance.
metrics = PlatformMetrics()
