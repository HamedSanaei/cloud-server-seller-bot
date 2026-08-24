"""OpenTelemetry tracing (M11-002).

Acceptance: trace spans correlate API -> job -> provider.

The correlation chain:

1. **API span** - the API middleware starts one span per HTTP request
   (joining an inbound W3C ``traceparent`` when present).
2. **Job/operation span** - when a command enqueues a durable operation
   (create/power/delete), the operation row captures the CURRENT
   ``traceparent`` at creation time. The worker starts its execution span
   as a child of that persisted remote parent, so the operation lands in
   the SAME trace as the API request that caused it - across processes.
3. **Provider span** - every provider API call opens a span (child of
   whatever executes it: the API request or the worker operation), so a
   single trace shows request -> operation -> provider call.

The module is dependency-free at import time: without ``setup_tracing``
the tracer API is a no-op (no spans recorded, no errors), so a process
with tracing disabled behaves exactly as before.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter  # noqa: F401  (used by tests)
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.trace import NonRecordingSpan, Span, SpanContext, TraceFlags, get_current_span

__all__ = [
    "api_span_attributes",
    "current_traceparent",
    "get_tracer",
    "job_span",
    "operation_span",
    "parse_traceparent",
    "provider_span",
    "remote_parent_context",
    "set_span_status_error",
    "setup_tracing",
]

_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def parse_traceparent(value: str) -> tuple[int, int, int] | None:
    """Parse a W3C ``traceparent`` into (trace_id, span_id, flags).

    Returns None for anything malformed - a bad persisted value must never
    break execution (the span simply starts as a new root).
    """
    match = _TRACEPARENT_RE.match((value or "").strip())
    if match is None:
        return None
    trace_id = int(match.group(1), 16)
    span_id = int(match.group(2), 16)
    flags = int(match.group(3), 16)
    if trace_id == 0 or span_id == 0:
        return None
    return trace_id, span_id, flags


def current_traceparent() -> str | None:
    """The W3C traceparent of the CURRENT span, or None when there is no
    valid span in context (e.g. tracing disabled or outside a span)."""
    span_context = get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    trace_id = format(span_context.trace_id, "032x")
    span_id = format(span_context.span_id, "016x")
    flags = format(span_context.trace_flags, "02x")
    return f"00-{trace_id}-{span_id}-{flags}"


def remote_parent_context(traceparent: str | None) -> Any | None:
    """A context in which a NEW span becomes a child of the persisted
    remote span (the API request that enqueued the operation)."""
    parsed = parse_traceparent(traceparent or "")
    if parsed is None:
        return None
    trace_id, span_id, flags = parsed
    remote = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(flags),
    )
    return trace.set_span_in_context(NonRecordingSpan(remote))


def setup_tracing(
    service_name: str,
    *,
    otlp_endpoint: str = "",
    sample_ratio: float = 1.0,
) -> TracerProvider:
    """Install the process-wide tracer provider.

    With ``otlp_endpoint`` set, spans batch-export over OTLP/GRPC to the
    collector; without it, spans are still created in-process (context
    propagation and correlation work) but not exported.

    The first call in a process wins: the OTel API forbids overriding an
    installed provider, so later calls (e.g. the API app after a test) are
    no-ops returning the installed provider.
    """
    from opentelemetry.propagate import set_global_textmap
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import get_tracer_provider, set_tracer_provider
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    installed = get_tracer_provider()
    if isinstance(installed, TracerProvider):
        # A provider is already installed (e.g. another app factory in tests
        # or a previous process bootstrap): keep it - override is not allowed.
        return installed
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=TraceIdRatioBased(sample_ratio),
    )
    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    set_tracer_provider(provider)
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator()]))
    return provider


def get_tracer(instrumenting_scope: str) -> trace.Tracer:
    """The process tracer for one instrumentation scope (no-op safe)."""
    return trace.get_tracer(f"cloud_platform.{instrumenting_scope}")


@contextmanager
def provider_span(provider: str, operation: str) -> Iterator[Span]:
    """One provider API call as a span (child of the current context)."""
    tracer = get_tracer("providers")
    with tracer.start_as_current_span(f"provider call {operation}") as span:
        span.set_attribute("cloud.provider", provider)
        span.set_attribute("cloud.provider.operation", operation)
        yield span


@contextmanager
def job_span(job: str) -> Iterator[Span]:
    """One background job run as a span (root when no ambient context)."""
    tracer = get_tracer("jobs")
    with tracer.start_as_current_span(f"job {job}") as span:
        span.set_attribute("cloud.job", job)
        yield span


@asynccontextmanager
async def operation_span(
    name: str,
    *,
    traceparent: str | None,
    attributes: dict[str, str | int] | None = None,
) -> AsyncIterator[Span]:
    """One durable-operation execution as a span (M11-002).

    Joins the trace persisted in ``traceparent`` (the request that enqueued
    the operation) so one trace shows API -> job -> provider; a missing or
    malformed value simply makes the span a child of the current context.
    The error status is recorded when the body raises.
    """
    tracer = get_tracer("operations")
    parent_ctx = remote_parent_context(traceparent)
    with tracer.start_as_current_span(
        name,
        context=parent_ctx,
        attributes=attributes,
    ) as span:
        try:
            yield span
        except Exception as exc:
            set_span_status_error(span, exc)
            raise


def api_span_attributes(span: Span, method: str, path: str, status_code: int) -> None:
    """Bounded-cardinality HTTP attributes for the API request span."""
    span.set_attribute("http.method", method)
    span.set_attribute("http.status_code", status_code)
    span.set_attribute("http.route", path)


def set_span_status_error(span: Span, error: BaseException) -> None:
    """Record an exception on a span (message only - no secret material)."""
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.ERROR, type(error).__name__))
    span.record_exception(error)
