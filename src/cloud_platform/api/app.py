import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST

from cloud_platform.api.routes.health import router as health_router
from cloud_platform.api.routes.webhooks import router as webhooks_router
from cloud_platform.observability.metrics import metrics


def _route_path(request: Request) -> str:
    """The matched route template (bounded cardinality), else the raw path."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return str(route.path)
    return request.url.path


async def _observe_api(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    response: Response = await call_next(request)
    path = _route_path(request)
    status = str(response.status_code)
    metrics.api_requests_total.labels(method=request.method, path=path, status=status).inc()
    metrics.api_request_duration_seconds.labels(method=request.method, path=path).observe(
        time.perf_counter() - started
    )
    return response


def create_app() -> FastAPI:
    app = FastAPI(title="Cloud Server Platform API", version="0.1.0")
    app.middleware("http")(_observe_api)
    app.include_router(health_router)
    app.include_router(webhooks_router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        return Response(content=metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app
