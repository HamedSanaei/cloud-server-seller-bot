import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST

from cloud_platform.api.routes.health import router as health_router
from cloud_platform.api.routes.webhooks import router as webhooks_router
from cloud_platform.observability.metrics import metrics
from cloud_platform.observability.tracing import (
    api_span_attributes,
    get_tracer,
    remote_parent_context,
    set_span_status_error,
)


def _route_path(request: Request) -> str:
    """The matched route template (bounded cardinality), else the raw path."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return str(route.path)
    return request.url.path


def _setup_otel(app: FastAPI) -> None:
    """Install tracing for the API process (M11-002).

    Idempotent and config-driven: with no ``otel_exporter_endpoint`` set the
    process still gets in-process spans + W3C context propagation, just no
    OTLP export.
    """
    from cloud_platform.core.config import get_settings
    from cloud_platform.observability.tracing import setup_tracing

    settings = get_settings()
    setup_tracing(
        "cloud-platform-api",
        otlp_endpoint=settings.otel_exporter_endpoint,
        sample_ratio=settings.otel_sample_ratio,
    )
    app.state.otel_ready = True


async def _observe_api(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """One API request: Prometheus metrics + one trace span (M11-002).

    An inbound W3C ``traceparent`` header joins the caller's trace; the
    span becomes the parent of everything the request enqueues (operations
    capture its traceparent at creation time).
    """
    started = time.perf_counter()
    path = _route_path(request)
    tracer = get_tracer("api")
    parent_ctx = remote_parent_context(request.headers.get("traceparent"))
    with tracer.start_as_current_span(
        "API request",
        context=parent_ctx,
    ) as span:
        try:
            response: Response = await call_next(request)
        except Exception as exc:
            set_span_status_error(span, exc)
            raise
        status = str(response.status_code)
        api_span_attributes(span, request.method, path, int(status))
        metrics.api_requests_total.labels(method=request.method, path=path, status=status).inc()
        metrics.api_request_duration_seconds.labels(method=request.method, path=path).observe(
            time.perf_counter() - started
        )
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="Cloud Server Platform API", version="0.1.0")
    _setup_otel(app)
    app.middleware("http")(_observe_api)
    app.include_router(health_router)
    app.include_router(webhooks_router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app
