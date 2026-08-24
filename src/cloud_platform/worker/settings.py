from typing import Any, ClassVar

from arq.connections import RedisSettings

from cloud_platform.core.config import get_settings
from cloud_platform.observability.metrics import metrics


async def startup(ctx: dict[str, object]) -> None:
    ctx["service"] = "cloud-platform-worker"


async def shutdown(ctx: dict[str, object]) -> None:
    ctx.clear()


async def reconcile_provider_resources(ctx: dict[str, object]) -> None:
    # M07 implements bounded reconciliation batches with provider rate-limit awareness.
    del ctx
    async with metrics.job("reconcile_provider_resources"):
        pass


async def accrue_usage(ctx: dict[str, object]) -> None:
    """Settle complete usage periods for all RUNNING servers (M06-005).

    Scheduled periodically (arq cron); the advisory lock inside the job makes
    overlapping schedules safe.
    """
    del ctx
    async with metrics.job("accrue_usage"):
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
        from cloud_platform.modules.billing.repository import (
            PostgresAdvisoryAccrualLock,
            SqlAlchemyAccrualPeriodRepository,
        )
        from cloud_platform.modules.billing.service import AccrualJob
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.pricing.repository import (
            SqlAlchemyServerPriceSnapshotRepository,
        )
        from cloud_platform.modules.wallet.repository import (
            HoldService,
            SqlAlchemyHoldRepository,
            SqlAlchemyLedgerRepository,
            SqlAlchemyWalletRepository,
        )

        job = AccrualJob(
            server_repo=SqlAlchemyServerRepository(SessionFactory),
            wallet_repo=SqlAlchemyWalletRepository(SessionFactory),
            hold_repo=SqlAlchemyHoldRepository(SessionFactory),
            hold_service=HoldService(
                SqlAlchemyWalletRepository(SessionFactory),
                SqlAlchemyHoldRepository(SessionFactory),
                SqlAlchemyLedgerRepository(SessionFactory),
            ),
            ledger_repo=SqlAlchemyLedgerRepository(SessionFactory),
            accrual_repo=SqlAlchemyAccrualPeriodRepository(SessionFactory),
            snapshot_repo=SqlAlchemyServerPriceSnapshotRepository(SessionFactory),
            audit_repo=SqlAlchemyAuditRepository(SessionFactory),
            lock=PostgresAdvisoryAccrualLock(SessionFactory),
        )
        await job.run()


async def evaluate_low_balance(ctx: dict[str, object]) -> None:
    """Run the low-balance warn/grace/auto-delete policy (M06-007).

    Scheduled periodically (arq cron); per-server failures are counted in
    the report and never break the pass.
    """
    del ctx
    async with metrics.job("evaluate_low_balance"):
        from cloud_platform.core.config import get_settings
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
        from cloud_platform.modules.billing.service import (
            LowBalancePolicyConfig,
            LowBalancePolicyService,
        )
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

        settings = get_settings()
        service = LowBalancePolicyService(
            server_repo=SqlAlchemyServerRepository(SessionFactory),
            wallet_repo=SqlAlchemyWalletRepository(SessionFactory),
            audit_repo=SqlAlchemyAuditRepository(SessionFactory),
        )
        config = LowBalancePolicyConfig(
            threshold_minor=settings.low_balance_threshold_minor,
            grace_hours=settings.low_balance_grace_hours,
        )
        await service.evaluate(config)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        reconcile_provider_resources,
        accrue_usage,
        evaluate_low_balance,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 120
