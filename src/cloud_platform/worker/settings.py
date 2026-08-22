from typing import ClassVar

from arq.connections import RedisSettings

from cloud_platform.core.config import get_settings


async def startup(ctx: dict[str, object]) -> None:
    ctx["service"] = "cloud-platform-worker"


async def shutdown(ctx: dict[str, object]) -> None:
    ctx.clear()


async def reconcile_provider_resources(ctx: dict[str, object]) -> None:
    # M07 implements bounded reconciliation batches with provider rate-limit awareness.
    del ctx


class WorkerSettings:
    functions: ClassVar[list] = [reconcile_provider_resources]  # type: ignore[type-arg]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 120
