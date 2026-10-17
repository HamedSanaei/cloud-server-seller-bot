import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from cloud_platform.core.container import Container, get_container
from cloud_platform.core.health import PostgresReadiness, RedisReadiness, check_readiness

router = APIRouter(tags=["health"])

READINESS_TIMEOUT_SECONDS = 3


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(container: Annotated[Container, Depends(get_container)]) -> JSONResponse:
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            report = await check_readiness(
                (
                    PostgresReadiness(container.session_factory),
                    RedisReadiness(container.session_store()),
                )
            )
    except TimeoutError:
        return JSONResponse({"status": "unready"}, status_code=503)
    # Dependency exceptions can contain hostnames, DSNs or credentials. Expose
    # only the result; the cached container owns pools and their shutdown.
    return JSONResponse(
        {
            "status": "ok" if report.ready else "unready",
            "checks": [
                {"name": result.name, "status": result.status.value} for result in report.results
            ],
        },
        status_code=200 if report.ready else 503,
    )
