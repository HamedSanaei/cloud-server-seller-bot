"""Tests for OpenTelemetry tracing correlation (M11-002).

Acceptance: trace spans correlate API -> job -> provider.

The suite verifies the three links with a real SDK TracerProvider + an
in-memory exporter:

1. API middleware: one span per request, joining an inbound traceparent.
2. Operation rows capture the current traceparent at creation time; the
   worker's execution span joins that persisted trace (same trace id).
3. Provider calls open a span nested under the executing context, so one
   trace shows API -> job/operation -> provider.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ReadableSpan,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)

from cloud_platform.observability import tracing
from cloud_platform.observability.tracing import (
    current_traceparent,
    parse_traceparent,
    remote_parent_context,
    setup_tracing,
)


class _InMemoryExporter(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Any) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


@pytest.fixture(autouse=True)
def _ensure_provider() -> Iterator[None]:
    """Guarantee an SDK TracerProvider is installed for this file's tests.

    The OTel API is a no-op until a provider is installed; the first
    setup_tracing call in the process wins, so this is safe to run after
    other test files have installed one.
    """
    setup_tracing("test-service")
    yield


@pytest.fixture()
def exporter() -> Iterator[_InMemoryExporter]:
    """A guaranteed-exporting in-memory exporter on the process provider.

    The SDK provider is process-wide and first-install-wins, so each test
    appends a fresh processor (and clears its exporter) - processors can be
    added at any time and spans flow from that moment on.
    """
    exp = _InMemoryExporter()
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    provider.add_span_processor(SimpleSpanProcessor(exp))
    yield exp


def _by_name(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


# ---------------------------------------------------------------------------
# W3C traceparent plumbing
# ---------------------------------------------------------------------------


class TestTraceparent:
    def test_parse_valid(self) -> None:
        got = parse_traceparent("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
        assert got == (0x0AF7651916CD43DD8448EB211C80319C, 0xB7AD6B7169203331, 0x01)

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "garbage",
            "00-xyz-b7ad6b7169203331-01",
            "00-0af7651916cd43dd8448eb211c80319c-00-01",
            "00-00000000000000000000000000000000-b7ad6b7169203331-01",
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-000",
            "01-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        ],
    )
    def test_parse_invalid(self, bad: str) -> None:
        assert parse_traceparent(bad) is None

    def test_current_outside_span_is_none(self) -> None:
        assert current_traceparent() is None

    def test_current_inside_span(self) -> None:
        tracer = tracing.get_tracer("test")
        with tracer.start_as_current_span("root"):
            tp = current_traceparent()
        assert tp is not None
        parsed = parse_traceparent(tp)
        assert parsed is not None

    def test_remote_parent_context_roundtrip(self) -> None:
        tracer = tracing.get_tracer("test")
        with tracer.start_as_current_span("parent"):
            tp = current_traceparent()
        assert tp is not None
        parent_parsed = parse_traceparent(tp)
        assert parent_parsed is not None
        ctx = remote_parent_context(tp)
        assert ctx is not None
        child = tracer.start_span("child", context=ctx)
        child_ctx = child.get_span_context()
        assert child_ctx.trace_id == parent_parsed[0]
        assert child_ctx.span_id != parent_parsed[1]
        child.end()

    def test_remote_parent_context_bad_value_is_none(self) -> None:
        assert remote_parent_context("not-a-traceparent") is None
        assert remote_parent_context(None) is None


# ---------------------------------------------------------------------------
# 1. API middleware span
# ---------------------------------------------------------------------------


class TestApiSpan:
    def test_request_creates_a_span(self, exporter: _InMemoryExporter) -> None:
        from cloud_platform.api.app import create_app

        exporter.spans.clear()
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/health/live")
        assert resp.status_code == 200
        api_spans = _by_name(exporter.spans, "API request")
        assert len(api_spans) == 1
        span = api_spans[0]
        assert span.attributes.get("http.method") == "GET"
        assert span.attributes.get("http.status_code") == 200
        assert span.attributes.get("http.route") is not None

    def test_inbound_traceparent_joins_caller_trace(self, exporter: _InMemoryExporter) -> None:
        from cloud_platform.api.app import create_app

        # a foreign trace the caller started
        foreign_tp = "00-11111111111111111111111111111111-2222222222222222-01"
        exporter.spans.clear()
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/health/live", headers={"traceparent": foreign_tp})
        assert resp.status_code == 200
        api_spans = _by_name(exporter.spans, "API request")
        assert len(api_spans) == 1
        assert api_spans[0].context.trace_id == 0x11111111111111111111111111111111


# ---------------------------------------------------------------------------
# 2. Operation row captures traceparent; worker span joins the trace
# ---------------------------------------------------------------------------


class TestOperationCorrelation:
    async def test_worker_operation_span_joins_api_trace(self, exporter: _InMemoryExporter) -> None:
        """The acceptance chain: API span -> (persisted traceparent) ->
        worker operation span in the SAME trace, with a provider span nested."""
        exporter.spans.clear()
        tracer = tracing.get_tracer("api")

        # --- API side: the request span that enqueues the operation --------
        with tracer.start_as_current_span("API request"):
            api_tp = current_traceparent()
        assert api_tp is not None

        # --- persisted onto the operation row (as the repository does) -----
        from uuid import uuid4

        from cloud_platform.modules.operations.domain import (
            Operation,
            OperationType,
        )

        operation = Operation(
            id=uuid4(),
            operation_key="ik-power-1",
            operation_type=OperationType.POWER_ON,
            resource_type="server",
            resource_id=uuid4(),
            provider_key="hetzner",
            traceparent=api_tp,
        )

        # --- worker side: the execution span joins the persisted trace -----
        async with tracing.operation_span(
            "power operation",
            traceparent=operation.traceparent,
            attributes={"cloud.operation.id": str(operation.id)},
        ):
            # --- provider side: nested under the operation span ------------
            with tracing.provider_span("hetzner", "power_server"):
                pass

        # all three spans in ONE trace, correctly parented
        trace_id = parse_traceparent(api_tp)[0]
        api_spans = _by_name(exporter.spans, "API request")
        op_spans = _by_name(exporter.spans, "power operation")
        prov_spans = _by_name(exporter.spans, "provider call power_server")
        assert len(api_spans) == 1 and len(op_spans) == 1 and len(prov_spans) == 1

        assert api_spans[0].context.trace_id == trace_id
        assert op_spans[0].context.trace_id == trace_id  # same trace across processes
        assert prov_spans[0].context.trace_id == trace_id

        # parentage: operation child of the API span; provider child of op
        assert op_spans[0].parent is not None
        assert op_spans[0].parent.span_id == api_spans[0].context.span_id
        assert op_spans[0].parent.is_remote  # it crossed a process boundary
        assert prov_spans[0].parent is not None
        assert prov_spans[0].parent.span_id == op_spans[0].context.span_id
        assert not prov_spans[0].parent.is_remote

    async def test_operation_without_traceparent_is_root(self, exporter: _InMemoryExporter) -> None:
        exporter.spans.clear()
        async with tracing.operation_span("power operation", traceparent=None):
            pass
        op_spans = _by_name(exporter.spans, "power operation")
        assert len(op_spans) == 1
        assert op_spans[0].parent is None  # no persisted parent -> root span

    async def test_malformed_traceparent_does_not_break(self, exporter: _InMemoryExporter) -> None:
        exporter.spans.clear()
        async with tracing.operation_span("power operation", traceparent="garbage"):
            pass
        assert len(_by_name(exporter.spans, "power operation")) == 1

    async def test_operation_span_records_error(self, exporter: _InMemoryExporter) -> None:
        exporter.spans.clear()
        with pytest.raises(ValueError):
            async with tracing.operation_span("power operation", traceparent=None):
                raise ValueError("boom")
        op_spans = _by_name(exporter.spans, "power operation")
        assert len(op_spans) == 1
        assert op_spans[0].status.status_code.name == "ERROR"


# ---------------------------------------------------------------------------
# 3. provider_call / job helpers open spans
# ---------------------------------------------------------------------------


class TestHelperSpans:
    async def test_provider_call_opens_nested_span(self, exporter: _InMemoryExporter) -> None:
        from cloud_platform.observability.metrics import PlatformMetrics

        exporter.spans.clear()
        m = PlatformMetrics()
        tracer = tracing.get_tracer("test")
        with tracer.start_as_current_span("parent"):
            async with m.provider_call("hetzner", "list_servers"):
                pass
        parent_spans = _by_name(exporter.spans, "parent")
        call_spans = _by_name(exporter.spans, "provider call list_servers")
        assert len(parent_spans) == 1 and len(call_spans) == 1
        assert call_spans[0].parent is not None
        assert call_spans[0].parent.span_id == parent_spans[0].context.span_id
        assert call_spans[0].attributes.get("cloud.provider") == "hetzner"

    async def test_provider_call_error_span(self, exporter: _InMemoryExporter) -> None:
        from cloud_platform.observability.metrics import PlatformMetrics
        from cloud_platform.providers.errors import ProviderRateLimited

        exporter.spans.clear()
        m = PlatformMetrics()
        with pytest.raises(ProviderRateLimited):
            async with m.provider_call("arvancloud", "create_server"):
                raise ProviderRateLimited("429")
        call_spans = _by_name(exporter.spans, "provider call create_server")
        assert len(call_spans) == 1
        assert call_spans[0].status.status_code.name == "ERROR"
        assert call_spans[0].attributes.get("cloud.provider.outcome") == "rate_limited"

    async def test_job_span(self, exporter: _InMemoryExporter) -> None:
        from cloud_platform.observability.metrics import PlatformMetrics

        exporter.spans.clear()
        m = PlatformMetrics()
        async with m.job("accrue_usage"):
            pass
        job_spans = _by_name(exporter.spans, "job accrue_usage")
        assert len(job_spans) == 1
        assert job_spans[0].attributes.get("cloud.job") == "accrue_usage"


# ---------------------------------------------------------------------------
# setup_tracing idempotence
# ---------------------------------------------------------------------------


class TestSetup:
    def test_second_setup_returns_installed_provider(self) -> None:
        first = setup_tracing("svc-a")
        second = setup_tracing("svc-b", otlp_endpoint="http://collector:4317")
        assert first is second
