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
        from datetime import datetime
        from uuid import UUID

        from cloud_platform.core.config import get_settings
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
        from cloud_platform.modules.billing.service import (
            LowBalanceDecision,
            LowBalancePolicyConfig,
            LowBalancePolicyService,
        )
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.notifications.domain import (
            LowBalanceNotifier,
            _LoggingLowBalanceNotifier,
        )
        from cloud_platform.modules.notifications.repository import (
            SqlAlchemyLowBalanceNotificationLogRepository,
        )
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

        settings = get_settings()
        currency = settings.default_currency
        low_balance_notifier = LowBalanceNotifier(
            notifier=_LoggingLowBalanceNotifier(),
            log_repo=SqlAlchemyLowBalanceNotificationLogRepository(SessionFactory),
        )

        class _LowBalanceNotifierAdapter:
            """Adapts the deduplicating LowBalanceNotifier to the BalanceNotifier port."""

            async def notify(
                self,
                user_id: UUID,
                server_id: UUID,
                decision: LowBalanceDecision,
                balance_minor: int,
                episode: datetime | None = None,
            ) -> None:
                if episode is None:
                    # The policy always passes the watermark on notified
                    # decisions; without an episode there is nothing to dedup by.
                    return
                await low_balance_notifier.notify(
                    user_id, server_id, decision, balance_minor, currency, episode
                )

        service = LowBalancePolicyService(
            server_repo=SqlAlchemyServerRepository(SessionFactory),
            wallet_repo=SqlAlchemyWalletRepository(SessionFactory),
            audit_repo=SqlAlchemyAuditRepository(SessionFactory),
            notifier=_LowBalanceNotifierAdapter(),
        )
        config = LowBalancePolicyConfig(
            threshold_minor=settings.low_balance_threshold_minor,
            grace_hours=settings.low_balance_grace_hours,
        )
        await service.evaluate(config)


async def process_deletes(ctx: dict[str, object]) -> None:
    """Run the delete-server saga for pending delete operations (M07-007).

    The command path executes deletions inline; this worker re-queues crash
    recovery and retryable failures (retryable provider errors, absence-wait
    timeouts). Scheduled periodically (arq cron).
    """
    del ctx
    async with metrics.job("process_deletes"):
        from cloud_platform.core.config import get_settings
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
        from cloud_platform.modules.billing.repository import (
            SqlAlchemyAccrualPeriodRepository,
        )
        from cloud_platform.modules.billing.service import FinalChargeService
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import (
            SqlAlchemyOperationRepository,
        )
        from cloud_platform.modules.operations.service import (
            DeleteOperationExecutor,
            DeleteWorker,
        )
        from cloud_platform.modules.pricing.repository import (
            SqlAlchemyServerPriceSnapshotRepository,
        )
        from cloud_platform.modules.wallet.repository import (
            HoldService,
            SqlAlchemyHoldRepository,
            SqlAlchemyLedgerRepository,
            SqlAlchemyWalletRepository,
        )
        from cloud_platform.providers.registry import ProviderRegistry

        settings = get_settings()
        registry = ProviderRegistry()
        if settings.hetzner_api_token:
            from cloud_platform.providers.hetzner.client import HetznerCloudProvider

            registry.register(
                HetznerCloudProvider(
                    token=settings.hetzner_api_token,
                    base_url=settings.hetzner_api_base_url,
                )
            )
        wallet_repo = SqlAlchemyWalletRepository(SessionFactory)
        hold_repo = SqlAlchemyHoldRepository(SessionFactory)
        hold_service = HoldService(
            wallet_repo, hold_repo, SqlAlchemyLedgerRepository(SessionFactory)
        )
        final_charge = FinalChargeService(
            server_repo=SqlAlchemyServerRepository(SessionFactory),
            wallet_repo=wallet_repo,
            hold_repo=hold_repo,
            hold_service=hold_service,
            ledger_repo=SqlAlchemyLedgerRepository(SessionFactory),
            accrual_repo=SqlAlchemyAccrualPeriodRepository(SessionFactory),
            snapshot_repo=SqlAlchemyServerPriceSnapshotRepository(SessionFactory),
            audit_repo=SqlAlchemyAuditRepository(SessionFactory),
        )
        executor = DeleteOperationExecutor(
            operation_repo=SqlAlchemyOperationRepository(SessionFactory),
            server_repo=SqlAlchemyServerRepository(SessionFactory),
            provider_registry=registry,
            final_charge=final_charge,
            hold_repo=hold_repo,
            hold_service=hold_service,
            wallet_repo=wallet_repo,
            audit_repo=SqlAlchemyAuditRepository(SessionFactory),
        )
        worker = DeleteWorker(
            operation_repo=SqlAlchemyOperationRepository(SessionFactory),
            executor=executor,
        )
        await worker.process_pending_deletes()


async def reconcile_deletes(ctx: dict[str, object]) -> None:
    """Reconcile ambiguous deletions (M07-008).

    Re-queues IN_FLIGHT delete operations that outlived their worker and
    recreates operations for servers stuck in DELETE_REQUESTED without one;
    the delete worker then re-enters the saga with the same keys (404 on
    delete is success; the final charge is idempotent). Scheduled
    periodically (arq cron).
    """
    del ctx
    async with metrics.job("reconcile_deletes"):
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
        from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
        from cloud_platform.modules.operations.repository import (
            SqlAlchemyOperationRepository,
        )
        from cloud_platform.modules.operations.service import DeleteTimeoutReconciler

        reconciler = DeleteTimeoutReconciler(
            operation_repo=SqlAlchemyOperationRepository(SessionFactory),
            server_repo=SqlAlchemyServerRepository(SessionFactory),
            audit_repo=SqlAlchemyAuditRepository(SessionFactory),
        )
        await reconciler.reconcile()


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        reconcile_provider_resources,
        accrue_usage,
        evaluate_low_balance,
        process_deletes,
        reconcile_deletes,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 120
