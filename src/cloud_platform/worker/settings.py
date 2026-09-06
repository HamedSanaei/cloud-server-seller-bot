import logging
from datetime import UTC, datetime
from typing import Any, ClassVar

from arq.connections import RedisSettings

from cloud_platform.core.config import get_settings
from cloud_platform.observability.metrics import metrics

logger = logging.getLogger(__name__)


async def startup(ctx: dict[str, object]) -> None:
    ctx["service"] = "cloud-platform-worker"
    # M11-002: install tracing for the worker process (no-op when a provider
    # is already installed; in-process spans without OTLP when no endpoint).
    from cloud_platform.core.config import get_settings
    from cloud_platform.observability.tracing import setup_tracing

    settings = get_settings()
    setup_tracing(
        "cloud-platform-worker",
        otlp_endpoint=settings.otel_exporter_endpoint,
        sample_ratio=settings.otel_sample_ratio,
    )


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
        from cloud_platform.modules.operations.domain import OperationType
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
        if settings.leaseweb_api_key:
            from cloud_platform.providers.leaseweb.client import LeaseWebProvider

            registry.register(
                LeaseWebProvider(
                    api_key=settings.leaseweb_api_key,
                    base_url=settings.leaseweb_api_base_url,
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
        # M11-006: the queue-age alert feed for the delete work queue.
        ops_repo = SqlAlchemyOperationRepository(SessionFactory)
        metrics.record_operation_queue_age(
            OperationType.SERVER_DELETE.value,
            await ops_repo.oldest_pending_age_seconds(
                [OperationType.SERVER_DELETE], datetime.now(UTC)
            ),
        )


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


def _telegram_notifiers() -> tuple[Any, Any] | None:
    """(delivery_notifier, renewal_notifier) when Telegram is configured."""
    from cloud_platform.core.config import get_settings

    settings = get_settings()
    if not settings.telegram_bot_token:
        return None
    from aiogram import Bot

    from cloud_platform.bot.notifier import (
        TelegramOrderDeliveryNotifier,
        TelegramRenewalNotifier,
    )
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.users.repository import SqlAlchemyUserRepository

    bot = Bot(token=settings.telegram_bot_token)
    users = SqlAlchemyUserRepository(SessionFactory)
    return (
        TelegramOrderDeliveryNotifier(bot, users),
        TelegramRenewalNotifier(bot, users, settings.telegram_admin_chat_id or None),
    )


async def sync_leaseweb_offers(ctx: dict[str, object]) -> None:
    """Refresh the sellable-offer price book from the Leaseweb ordering API.

    Scheduled daily (arq cron). Never touches operator-owned fields
    (enabled, selling price). Skipped when LEASEWEB_API_KEY is unset.
    """
    del ctx
    async with metrics.job("sync_leaseweb_offers"):
        from cloud_platform.core.config import get_settings
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.providers.leaseweb.ordering import LeaseWebOrderingProvider
        from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer

        settings = get_settings()
        if not settings.leaseweb_api_key:
            return
        provider = LeaseWebOrderingProvider(
            api_key=settings.leaseweb_api_key,
            base_url=settings.leaseweb_api_base_url,
            locations=tuple(
                part.strip()
                for part in (settings.leaseweb_locations or "").split(",")
                if part.strip()
            )
            or ("AMS-01", "FRA-01"),
            os_allowlist=tuple(
                part.strip()
                for part in (settings.leaseweb_os_allowlist or "").split(",")
                if part.strip()
            ),
            order_os_only_free=settings.leaseweb_order_os_only_free,
        )
        syncer = LeaseWebOrderingCatalogSyncer(SessionFactory, provider)
        result = await syncer.sync_all()
        for name, step in result.items():
            logger.info(
                "leaseweb offer sync %s: fetched=%s upserted=%s errors=%s",
                name,
                step.total_fetched,
                step.total_upserted,
                step.errors,
            )


async def process_leaseweb_orders(ctx: dict[str, object]) -> None:
    """Execute PENDING_SUBMIT order intents (the monthly provisioning worker).

    Scheduled periodically (arq cron). Claims each intent through the
    operation ledger (PENDING -> IN_FLIGHT) so two overlapping runs can
    never POST the same order; the deterministic operation key is the
    IdempotencyKey of the provider POST.
    """
    del ctx
    async with metrics.job("process_leaseweb_orders"):
        from cloud_platform.core.config import get_settings

        settings = get_settings()
        if not settings.leaseweb_api_key:
            return
        notifiers = _telegram_notifiers()
        from cloud_platform.core.container import create_container

        container = create_container()
        try:
            await container.initialize()  # registers the ordering provider
            worker = container.order_worker(delivery_notifier=notifiers[0] if notifiers else None)
            counts = await worker.process_pending(limit=10)
            logger.info("leaseweb order worker: %s", counts)
        finally:
            await container.close()


async def reconcile_leaseweb_orders(ctx: dict[str, object]) -> None:
    """Poll open provider orders + resolve OUTCOME_UNKNOWN orders with
    READ-ONLY recovery; NEVER POSTs anything (LEASEWEB-MVP)."""
    del ctx
    async with metrics.job("reconcile_leaseweb_orders"):
        from cloud_platform.core.config import get_settings

        settings = get_settings()
        if not settings.leaseweb_api_key:
            return
        notifiers = _telegram_notifiers()
        from cloud_platform.core.container import create_container

        container = create_container()
        try:
            await container.initialize()  # registers the ordering provider
            reconciler = container.order_reconciler(
                delivery_notifier=notifiers[0] if notifiers else None
            )
            counts = await reconciler.reconcile(limit=100)
            logger.info("leaseweb order reconciler: %s", counts)
            # Ambiguous-outcome orders are resolved by READ-ONLY scans only:
            # attach a proven provider order id, or escalate for a human.
            recovery = container.order_recovery()
            recovery_counts = await recovery.recover(limit=50)
            logger.info("leaseweb order recovery: %s", recovery_counts)
        finally:
            await container.close()


async def check_renewals(ctx: dict[str, object]) -> None:
    """The daily renewal pass: reminders, exactly-once charge, flags."""
    del ctx
    async with metrics.job("check_renewals"):
        notifiers = _telegram_notifiers()
        from cloud_platform.core.container import create_container

        container = create_container()
        try:
            checker = container.renewal_checker(
                user_notifier=notifiers[1] if notifiers else None,
                admin_notifier=notifiers[1] if notifiers else None,
            )
            outcomes = await checker.run()
            logger.info("renewal checker: %s outcomes", len(outcomes))
        finally:
            await container.close()


async def reconcile_payments(ctx: dict[str, object]) -> None:
    """Recheck stuck PENDING payment sessions (M09-007).

    Verifies each stuck session against its gateway and credits through the
    same replay-safe webhook service — a recheck can never double-deposit.
    Skipped entirely when no gateway is configured.
    """
    del ctx
    async with metrics.job("reconcile_payments"):
        from cloud_platform.core.config import get_settings
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
        from cloud_platform.modules.payments.reconcile import PaymentReconciliationService
        from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
        from cloud_platform.modules.payments.service import PaymentWebhookService
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyLedgerRepository,
            SqlAlchemyWalletRepository,
        )

        settings = get_settings()
        if not settings.zarinpal_merchant_id:
            return
        from cloud_platform.providers.zarinpal.client import ZarinPalGateway

        gateway = ZarinPalGateway(
            merchant_id=settings.zarinpal_merchant_id,
            base_url=settings.zarinpal_base_url,
            sandbox=settings.zarinpal_sandbox,
            callback_url=settings.zarinpal_callback_url,
        )
        try:
            service = PaymentReconciliationService(
                payments_repo=SqlAlchemyPaymentSessionRepository(SessionFactory),
                webhook_service=PaymentWebhookService(
                    payments_repo=SqlAlchemyPaymentSessionRepository(SessionFactory),
                    wallet_repo=SqlAlchemyWalletRepository(SessionFactory),
                    ledger_repo=SqlAlchemyLedgerRepository(SessionFactory),
                ),
                gateway=gateway,
                audit_repo=SqlAlchemyAuditRepository(SessionFactory),
            )
            await service.run()
        finally:
            await gateway.close()


def _cron_jobs() -> list[Any]:
    """Cron schedule for the LEASEWEB-MVP jobs (daily sync/renewal, periodic
    order worker/reconciler). Overlapping runs are safe: the order worker
    claims through the operation ledger and the reconciler never mutates."""
    from arq.cron import cron

    every_two_minutes = set(range(0, 60, 2))
    every_three_minutes = set(range(0, 60, 3))
    return [
        cron(sync_leaseweb_offers, hour={3}, minute={17}, run_at_startup=True),
        cron(process_leaseweb_orders, minute=every_two_minutes, run_at_startup=True),
        cron(reconcile_leaseweb_orders, minute=every_three_minutes, run_at_startup=True),
        cron(check_renewals, hour={3}, minute={23}, run_at_startup=True),
    ]


class WorkerSettings:
    """Default combined worker (dev / small deploys): every queue inline."""

    functions: ClassVar[list[Any]] = [
        reconcile_provider_resources,
        accrue_usage,
        evaluate_low_balance,
        process_deletes,
        reconcile_deletes,
        reconcile_payments,
        sync_leaseweb_offers,
        process_leaseweb_orders,
        reconcile_leaseweb_orders,
        check_renewals,
    ]
    cron_jobs: ClassVar[list[Any]] = _cron_jobs()
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    job_timeout = 120


# M16-004: queue partitioning by responsibility. Each role runs as its own
# arq worker process with an isolated queue so a billing backlog can never
# starve provisioning (and vice versa). Enqueue with
# ``await redis.enqueue_job("accrue_usage", _queue_name="billing")`` etc.
PROVISIONING_FUNCTIONS: list[Any] = [
    reconcile_provider_resources,
    process_deletes,
    reconcile_deletes,
]
BILLING_FUNCTIONS: list[Any] = [accrue_usage, evaluate_low_balance, reconcile_payments]


class ProvisioningWorkerSettings(WorkerSettings):
    """Provisioning queue: creates, deletes, provider reconciliation."""

    queue_name = "provisioning"
    functions: ClassVar[list[Any]] = PROVISIONING_FUNCTIONS
    max_jobs = 10


class BillingWorkerSettings(WorkerSettings):
    """Billing queue: accrual, low-balance policy, payment reconciliation."""

    queue_name = "billing"
    functions: ClassVar[list[Any]] = BILLING_FUNCTIONS
    max_jobs = 10


class NotifyWorkerSettings(WorkerSettings):
    """Notify queue: Telegram/low-balance notifications (same functions, own queue)."""

    queue_name = "notify"
    functions: ClassVar[list[Any]] = []
    max_jobs = 20


WORKER_QUEUES: tuple[str, ...] = ("provisioning", "billing", "notify")
