from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready() -> dict[str, str]:
    # M01 adds actual PostgreSQL/Redis/provider readiness checks.
    return {"status": "ok", "mode": "starter"}
