import logging
from datetime import UTC, datetime
from inspect import isawaitable
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


def _has_order_provider_credentials(settings: object) -> bool:
    """Whether any configured provider can own an order/recovery pass."""
    if any(
        str(getattr(settings, name, "") or "").strip()
        for name in ("leaseweb_api_key", "hetzner_api_token", "arvancloud_api_key")
    ):
        return True
    accounts = getattr(settings, "leaseweb_accounts", ())
    try:
        return any(bool(getattr(account, "usable", False)) for account in accounts)
    except TypeError:
        return bool(accounts)


async def reconcile_provider_resources(ctx: dict[str, object]) -> None:
    """Run the bounded provider-resource reconcilers in production.

    The scheduled job used to be a metrics-only placeholder, which left
    accepted hourly instances in PROVISIONING forever and never repaired a
    timed-out create.  Both reconcilers are read/reconciliation paths: they
    never issue an untracked provider mutation.
    """
    del ctx
    async with metrics.job("reconcile_provider_resources"):
        from cloud_platform.core.container import create_container
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.operations.repository import (
            SqlAlchemyOperationRepository,
        )
        from cloud_platform.modules.operations.service import (
            CreateTimeoutReconciler,
            FirstLinuxImageSelector,
            ServerStateReconciler,
        )
        from cloud_platform.modules.wallet.repository import SqlAlchemyHoldRepository

        container = create_container()
        try:
            await container.initialize()
            server_repo = container.server_repository()
            operation_repo = SqlAlchemyOperationRepository(SessionFactory)
            hold_repo = SqlAlchemyHoldRepository(SessionFactory)
            audit_repo = container.audit_repository()
            create_reconciler = CreateTimeoutReconciler(
                operation_repo=operation_repo,
                server_repo=server_repo,
                provider_registry=container.provider_registry,
                image_selector=FirstLinuxImageSelector(),
                wallet_repo=container.wallet_repository(),
                hold_repo=hold_repo,
                audit_repo=audit_repo,
            )
            create_report = await create_reconciler.reconcile()
            state_report = await ServerStateReconciler(
                server_repo=server_repo,
                provider_registry=container.provider_registry,
                audit_repo=audit_repo,
            ).reconcile()
            logger.info(
                "provider resource reconciliation: create=%s state=%s",
                create_report,
                state_report,
            )
        finally:
            await container.close()


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
        wallet_repository = SqlAlchemyWalletRepository(SessionFactory)
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
                wallet = await wallet_repository.get(user_id)
                if wallet is None:
                    return
                await low_balance_notifier.notify(
                    user_id,
                    server_id,
                    decision,
                    balance_minor,
                    wallet.currency,
                    episode,
                )

        service = LowBalancePolicyService(
            server_repo=SqlAlchemyServerRepository(SessionFactory),
            wallet_repo=wallet_repository,
            audit_repo=SqlAlchemyAuditRepository(SessionFactory),
            notifier=_LowBalanceNotifierAdapter(),
        )
        config = LowBalancePolicyConfig(
            threshold_minor=settings.low_balance_threshold_minor,
            grace_hours=settings.low_balance_grace_hours,
            currency=settings.fx_catalog_pricing_currency,
        )
        await service.evaluate(config)


async def process_deletes(ctx: dict[str, object]) -> None:
    owned_resources: list[Any] = []
    try:
        await _process_deletes_impl(ctx, owned_resources)
    finally:
        await _close_owned_resources(owned_resources)


async def _process_deletes_impl(ctx: dict[str, object], owned_resources: list[Any]) -> None:
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
            PostgresAdvisoryAccrualLock,
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

            hetzner_provider = HetznerCloudProvider(
                token=settings.hetzner_api_token,
                base_url=settings.hetzner_api_base_url,
            )
            owned_resources.append(hetzner_provider)
            registry.register(hetzner_provider)
        # LEASEWEB-MULTIACCOUNT: every configured credential account registers
        # as its own ROUTE under the ONE logical ``leaseweb`` key, each with its
        # own transport, so this worker resolves a server's pinned account
        # exactly like the API/bot processes do.
        from cloud_platform.providers.leaseweb.accounts import (
            build_leaseweb_account_router,
        )

        leaseweb_router = build_leaseweb_account_router(settings)
        if leaseweb_router is not None:
            owned_resources.append(leaseweb_router)
            for account_id, provider in leaseweb_router.new_order_clients():
                registry.register_route("leaseweb", account_id, provider)
            for account_id, provider in leaseweb_router.ordered_providers.items():
                if account_id not in registry.route_ids("leaseweb"):
                    registry.register_route("leaseweb", account_id, provider)
            registry.register_account_views("leaseweb", leaseweb_router.views())
        elif settings.leaseweb_api_key:
            from cloud_platform.providers.leaseweb.client import LeaseWebProvider

            leaseweb_provider = LeaseWebProvider(
                api_key=settings.leaseweb_api_key,
                base_url=settings.leaseweb_api_base_url,
            )
            owned_resources.append(leaseweb_provider)
            registry.register(leaseweb_provider)
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
            lock=PostgresAdvisoryAccrualLock(SessionFactory),
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


def _telegram_bot_token() -> str | None:
    """The configured bot token when it is *shaped* like a Telegram token.

    A placeholder or malformed token (``CHANGE_ME`` in a not-yet-edited
    ``configuration.toml``) must never crash a financial worker job:
    renewals, reconciliation and business-log delivery then simply run
    without Telegram. Availability of the chat channel is never allowed to
    break money movement.
    """
    from cloud_platform.core.config import get_settings

    token = (get_settings().telegram_bot_token or "").strip()
    prefix, sep, rest = token.partition(":")
    if not sep or not prefix.isdigit() or not rest or any(ch.isspace() for ch in token):
        if token:
            logger.warning("telegram bot token is unusable; Telegram is disabled")
        return None
    return token


def _telegram_notifiers() -> tuple[Any, Any] | None:
    """(delivery_notifier, renewal_notifier) when Telegram is configured."""
    token = _telegram_bot_token()
    if token is None:
        return None
    from cloud_platform.core.config import get_settings

    settings = get_settings()
    from aiogram import Bot

    from cloud_platform.bot.notifier import (
        TelegramOrderDeliveryNotifier,
        TelegramRenewalNotifier,
    )
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.users.repository import SqlAlchemyUserRepository

    bot = Bot(token=token)
    users = SqlAlchemyUserRepository(SessionFactory)
    return (
        TelegramOrderDeliveryNotifier(bot, users),
        TelegramRenewalNotifier(bot, users, settings.telegram_admin_chat_id or None),
    )


async def _close_telegram_notifiers(notifiers: tuple[Any, Any] | None) -> None:
    """Close the shared aiogram session created for a worker pass."""
    if not notifiers:
        return
    for notifier in notifiers:
        bot = getattr(notifier, "_bot", None)
        if bot is None:
            continue
        session = getattr(bot, "session", None)
        close = getattr(session, "close", None)
        if callable(close):
            try:
                await close()
            except Exception:
                logger.warning("worker Telegram session close failed", exc_info=True)


#: Built-in dedicated timeout (seconds) for one ``catalog_auto_sync`` run.
#: A Leaseweb refresh discovers hundreds of products across every account and
#: region with optional detail calls, which legitimately exceeds the generic
#: ``WorkerSettings.job_timeout``; the previous 120s cancellation killed the
#: run before pricing/publication ever executed.
CATALOG_AUTO_SYNC_TIMEOUT_SECONDS = 600


def catalog_auto_sync_timeout() -> int:
    """Dedicated ``catalog_auto_sync`` cron timeout in seconds.

    Only the catalog refresh is granted this budget: payment, order and
    reconciliation jobs keep the generic worker job timeout, so a stuck
    billing job can never hold a worker for minutes. A configured value is
    clamped to the sync interval — a job must not outlive its own cadence, and
    overlapping runs would only queue behind the catalog advisory lock.
    """
    from cloud_platform.core.config import get_settings

    default = CATALOG_AUTO_SYNC_TIMEOUT_SECONDS
    try:
        settings = get_settings()
        raw_timeout = settings.storefront_catalog_sync_timeout_seconds
        interval = int(settings.storefront_catalog_sync_interval_seconds)
    except Exception:
        return default
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int) or raw_timeout <= 0:
        logger.warning(
            "catalog auto-sync timeout %r is not a positive integer; using %ss",
            raw_timeout,
            default,
        )
        timeout = default
    else:
        timeout = raw_timeout
    if interval > 0 and timeout > interval:
        logger.warning(
            "catalog auto-sync timeout %ss exceeds the %ss sync interval; clamping to the interval",
            timeout,
            interval,
        )
        timeout = interval
    return timeout


def catalog_auto_sync_minutes() -> set[int]:
    """Cron minute set from the configured sync interval (default: 15 min).

    Only hour-dividing intervals are expressible as an arq minute set;
    anything else falls back to the default with a warning instead of
    silently running at an unexpected cadence.
    """
    from cloud_platform.core.config import get_settings

    try:
        interval = int(get_settings().storefront_catalog_sync_interval_seconds)
    except Exception:
        interval = 900
    if interval < 60 or 3600 % interval != 0:
        logger.warning(
            "catalog auto-sync interval %ss is not an hour-dividing minute cadence; "
            "using the 900s default",
            interval,
        )
        interval = 900
    return set(range(0, 60, interval // 60))


async def _close_owned_resources(resources: list[Any]) -> None:
    """Best-effort close for transports created by one catalog-sync run.

    Source construction happens before the coordinator enters its own
    ``try/finally``.  Keeping ownership in the job function means a failure
    while building the *next* source still closes routers/adapters created
    for earlier sources.  Routers are tracked rather than their child
    providers because their ``aclose`` method already owns those children.
    """
    seen: set[int] = set()
    for resource in reversed(resources):
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
        if not callable(closer):
            continue
        try:
            result = closer()
            if isawaitable(result):
                await result
        except Exception:
            logger.warning(
                "catalog sync owned resource close failed (%s)",
                type(resource).__name__,
                exc_info=True,
            )


async def sync_leaseweb_offers(ctx: dict[str, object]) -> None:
    """Refresh the sellable-offer price book from the Leaseweb ordering API.

    Kept for manual invocation; the periodic schedule runs
    :func:`catalog_auto_sync` (all providers plus pricing/publication).
    Never touches operator-owned fields (enabled, selling price). Skipped
    when LEASEWEB_API_KEY is unset.
    """
    del ctx
    async with metrics.job("sync_leaseweb_offers"):
        from cloud_platform.core.config import get_settings
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.providers.leaseweb.accounts import (
            build_leaseweb_account_router,
        )
        from cloud_platform.providers.leaseweb.ordering_sync import (
            LeaseWebOrderingCatalogSyncer,
            ordering_provider_from_settings,
        )

        settings = get_settings()
        owned_resources: list[Any] = []
        try:
            router = build_leaseweb_account_router(settings)
            if router is not None:
                owned_resources.append(router)
                # Accounts are processed SEQUENTIALLY on purpose: each one already
                # respects its own throttle, and the discovery cost is
                # ``accounts x locations x products`` — running them concurrently
                # here would multiply Leaseweb pressure for no operational gain.
                syncer = LeaseWebOrderingCatalogSyncer(
                    SessionFactory,
                    accounts=router.ordered_providers,
                    account_priorities=router.priorities,
                    account_states=router.account_states,
                )
            elif settings.leaseweb_api_key:
                provider = ordering_provider_from_settings(settings)
                owned_resources.append(provider)
                syncer = LeaseWebOrderingCatalogSyncer(SessionFactory, provider)
            else:
                return
            result = await syncer.sync_all()
            for name, step in result.items():
                logger.info(
                    "leaseweb offer sync %s: fetched=%s upserted=%s errors=%s",
                    name,
                    step.total_fetched,
                    step.total_upserted,
                    step.errors,
                )
        finally:
            await _close_owned_resources(owned_resources)


async def catalog_auto_sync(ctx: dict[str, object]) -> None:
    """Provider-neutral periodic offer refresh: sync, price, publish.

    One coordinator run over every configured provider (Leaseweb
    multi-account ordering, Hetzner Cloud): official read-only APIs refresh
    costs and availability, the server-owned markup policy reprices
    auto-priced rows, and eligible rows are published unless the operator
    blocked them. A provider failure is isolated (logged, never raised), an
    operator-disabled provider is skipped, and overlapping runs serialize on
    the catalog advisory lock. Runs at the configured interval with a prompt
    initial pass (``run_at_startup``) that never blocks process readiness
    (arq executes it as a job after startup).
    """
    del ctx
    async with metrics.job("catalog_auto_sync"):
        from cloud_platform.core.config import get_settings
        from cloud_platform.core.container import Container, create_container
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.catalog.repository import (
            PostgresAdvisoryCatalogSyncLock,
        )
        from cloud_platform.modules.offers.auto_sync import (
            CatalogAutoSyncCoordinator,
            pricing_policies_from_settings,
        )
        from cloud_platform.modules.offers.repository import (
            SqlAlchemyCatalogSyncStateRepository,
            SqlAlchemySellableOfferRepository,
        )

        settings = get_settings()
        owned_resources: list[Any] = []
        container: Any | None = None
        global_fx: Any | None = None
        try:
            if not settings.storefront_catalog_sync_enabled:
                logger.info("catalog auto-sync disabled by configuration; skipping")
                return
            sources: list[Any] = []
            if settings.providers_enabled.get("leaseweb", True):
                from cloud_platform.providers.leaseweb.accounts import (
                    build_leaseweb_account_router,
                )
                from cloud_platform.providers.leaseweb.auto_sync import (
                    LeasewebCatalogSyncSource,
                )
                from cloud_platform.providers.leaseweb.ordering_sync import (
                    LeaseWebOrderingCatalogSyncer,
                    ordering_provider_from_settings,
                )

                router = build_leaseweb_account_router(settings)
                if router is not None:
                    owned_resources.append(router)
                    sources.append(
                        LeasewebCatalogSyncSource(
                            LeaseWebOrderingCatalogSyncer(
                                SessionFactory,
                                accounts=router.ordered_providers,
                                account_priorities=router.priorities,
                                account_states=router.account_states,
                            )
                        )
                    )
                elif settings.leaseweb_api_key:
                    ordering_provider = ordering_provider_from_settings(settings)
                    owned_resources.append(ordering_provider)
                    sources.append(
                        LeasewebCatalogSyncSource(
                            LeaseWebOrderingCatalogSyncer(SessionFactory, ordering_provider)
                        )
                    )
                else:
                    logger.info("catalog auto-sync: leaseweb has no credential; skipping provider")
            else:
                logger.info("catalog auto-sync: leaseweb disabled by configuration; skipping")
            if settings.providers_enabled.get("leaseweb", True):
                from cloud_platform.providers.leaseweb.cloud_accounts import (
                    build_cloud_account_router,
                )
                from cloud_platform.providers.leaseweb.cloud_auto_sync import (
                    LeasewebHourlyCloudSyncSource,
                )
                from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

                # Account-aware Public Cloud discovery: every ACTIVE credential
                # account is probed; regions/types are attributed per account and
                # the hourly offer pins the owning account. Never the first key
                # by accident — the router owns the account set.
                cloud_router = build_cloud_account_router(settings)
                if cloud_router is not None:
                    owned_resources.append(cloud_router)
                    sources.append(
                        LeasewebHourlyCloudSyncSource(
                            LeasewebHourlyCloudSyncer(
                                SessionFactory,
                                accounts=dict(cloud_router.providers),
                                account_priorities=cloud_router.priorities,
                                account_states=cloud_router.account_states,
                            )
                        )
                    )
                else:
                    logger.info(
                        "catalog auto-sync: leaseweb hourly cloud has no credential; "
                        "skipping hourly sync (VPS ordering is unaffected)"
                    )
            if settings.providers_enabled.get("hetzner", True):
                if settings.hetzner_api_token:
                    from cloud_platform.providers.hetzner.auto_sync import (
                        HetznerCatalogSyncSource,
                    )
                    from cloud_platform.providers.hetzner.sync import HetznerCatalogSyncer

                    hetzner_syncer = HetznerCatalogSyncer(
                        SessionFactory,
                        catalog_currency=settings.fx_catalog_pricing_currency,
                        catalog_stale_limit_seconds=(
                            settings.fx_frankfurter_catalog_max_stale_seconds
                        ),
                    )
                    owned_resources.append(hetzner_syncer)
                    sources.append(HetznerCatalogSyncSource(hetzner_syncer))
                else:
                    logger.info(
                        "catalog auto-sync: hetzner credential missing; skipping provider "
                        "(leaseweb is unaffected)"
                    )
            else:
                logger.info("catalog auto-sync: hetzner disabled by configuration; skipping")
            if not sources:
                logger.info("catalog auto-sync: no providers configured; nothing to do")
                return
            container = create_container()
            global_fx = container.global_fx_resolver_or_none()
            coordinator = CatalogAutoSyncCoordinator(
                sources=sources,
                offers=SqlAlchemySellableOfferRepository(
                    SessionFactory,
                    catalog_currency=settings.fx_catalog_pricing_currency,
                    catalog_stale_limit_seconds=settings.fx_frankfurter_catalog_max_stale_seconds,
                ),
                state=SqlAlchemyCatalogSyncStateRepository(SessionFactory),
                lock=PostgresAdvisoryCatalogSyncLock(SessionFactory),
                pricing_policies=pricing_policies_from_settings(settings),
                reference_rates=global_fx,
                catalog_currency=settings.fx_catalog_pricing_currency,
                identity_ttl_seconds=settings.fx_frankfurter_quote_ttl_seconds,
            )
            report = await coordinator.run()
            if not report.ran:
                logger.info("catalog auto-sync skipped: %s", report.reason)
                return
            for provider in report.providers:
                logger.info(
                    "catalog auto-sync %s: ok=%s discovered=%d persisted=%d prices=%d "
                    "published=%d retired=%d warnings=%d errors=%d",
                    provider.provider_key,
                    provider.ok,
                    provider.discovered,
                    provider.persisted,
                    provider.prices_updated,
                    provider.published,
                    provider.retired,
                    len(provider.warnings),
                    len(provider.errors),
                )
        finally:
            if container is not None:
                try:
                    await container.close()
                except Exception:
                    logger.warning("catalog auto-sync container close failed", exc_info=True)
            if global_fx is not None:
                await Container.aclose_fx(global_fx)
            await _close_owned_resources(owned_resources)


async def process_cloud_creates(ctx: dict[str, object]) -> None:
    """Submit hourly cloud create intents (STOREFRONT-REWORK).

    Picks up REQUESTED hourly servers, claims each ``server-create``
    operation once, and POSTs the hourly instance exactly once per claimed
    operation. Ambiguous outcomes become OUTCOME_UNKNOWN (never a blind
    re-POST); the reconciler below attaches proven resources.
    """
    del ctx
    async with metrics.job("process_cloud_creates"):
        from cloud_platform.core.container import create_container

        container = create_container()
        try:
            await container.initialize()
            service = container.hourly_cloud_service()
            outcomes: dict[str, int] = {}
            for server in await service.servers_requested():
                try:
                    outcome = await service.process_server(server.id)
                except Exception as exc:
                    logger.warning("hourly create %s failed: %s", server.id, exc)
                    outcome = "error"
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcomes:
                logger.info("hourly creates processed: %s", outcomes)
        finally:
            await container.close()


async def reconcile_cloud_creates(ctx: dict[str, object]) -> None:
    """Attach proven instances to ambiguous hourly creates (read-only).

    For hourly servers stuck without a provider id, an exact reference
    match proves the earlier POST landed and is attached; anything else
    stays unknown for operator review (requeue re-POSTs safely through
    get-before-create).
    """
    del ctx
    async with metrics.job("reconcile_cloud_creates"):
        from cloud_platform.core.container import create_container

        container = create_container()
        try:
            await container.initialize()
            service = container.hourly_cloud_service()
            outcomes: dict[str, int] = {}
            for server in await service.servers_for_reconcile():
                try:
                    outcome = await service.reconcile_server(server.id)
                except Exception as exc:
                    logger.warning("hourly reconcile %s failed: %s", server.id, exc)
                    outcome = "error"
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcomes:
                logger.info("hourly creates reconciled: %s", outcomes)
        finally:
            await container.close()


async def process_leaseweb_orders(ctx: dict[str, object]) -> None:
    """Execute PENDING_SUBMIT order intents (the monthly provisioning worker).

    Scheduled periodically (arq cron). Claims each intent through the
    operation ledger (PENDING -> IN_FLIGHT) so two overlapping runs can
    never POST the same order; the deterministic operation key is the
    IdempotencyKey of the provider POST.
    """
    del ctx
    async with metrics.job("process_leaseweb_orders"):
        notifiers = _telegram_notifiers()
        from cloud_platform.core.config import get_settings as current_settings
        from cloud_platform.core.container import create_container

        if not _has_order_provider_credentials(current_settings()):
            logger.info("no provider credentials; skipping order submission")
            await _close_telegram_notifiers(notifiers)
            return
        container = create_container()
        try:
            await container.initialize()  # registers the ordering provider
            worker = container.order_worker(delivery_notifier=notifiers[0] if notifiers else None)
            counts = await worker.process_pending(limit=10)
            logger.info("leaseweb order worker: %s", counts)
        finally:
            await _close_telegram_notifiers(notifiers)
            await container.close()


async def reconcile_leaseweb_orders(ctx: dict[str, object]) -> None:
    """Poll open provider orders + resolve OUTCOME_UNKNOWN orders with
    READ-ONLY recovery; NEVER POSTs anything (LEASEWEB-MVP)."""
    del ctx
    async with metrics.job("reconcile_leaseweb_orders"):
        notifiers = _telegram_notifiers()
        from cloud_platform.core.config import get_settings as current_settings
        from cloud_platform.core.container import create_container

        if not _has_order_provider_credentials(current_settings()):
            logger.info("no provider credentials; skipping order reconciliation")
            await _close_telegram_notifiers(notifiers)
            return
        container = create_container()
        try:
            await container.initialize()  # registers the ordering provider
            reconciler = container.order_reconciler(
                delivery_notifier=notifiers[0] if notifiers else None
            )
            counts: dict[str, int] = {}
            for provider_key in container.provider_registry.keys():
                provider_counts = await reconciler.reconcile(provider_key=provider_key, limit=100)
                for outcome, count in provider_counts.items():
                    outcome_name = getattr(outcome, "value", str(outcome))
                    counts[str(outcome_name)] = counts.get(str(outcome_name), 0) + count
            logger.info("provider order reconciler: %s", counts)
            # Ambiguous-outcome orders are resolved by READ-ONLY scans only:
            # attach a proven provider order id, or escalate for a human.
            recovery = container.order_recovery()
            recovery_counts: dict[str, int] = {}
            for provider_key in container.provider_registry.keys():
                provider_recovery = await recovery.recover(provider_key=provider_key, limit=50)
                for outcome, count in provider_recovery.items():
                    outcome_name = getattr(outcome, "value", str(outcome))
                    recovery_counts[outcome_name] = recovery_counts.get(outcome_name, 0) + count
            logger.info("provider order recovery: %s", recovery_counts)
        finally:
            await _close_telegram_notifiers(notifiers)
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
            await _close_telegram_notifiers(notifiers)
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


async def reconcile_tetraminator_payments(ctx: dict[str, object]) -> None:
    """Recheck stuck Tetraminator PENDING sessions via read-only inquiry.

    Missed-callback safety net (Tetraminator webhooks are unsigned and may
    be lost): sufficiently old sessions carrying a pay_id are verified and
    credited through the same replay-safe webhook service. Skipped entirely
    when Tetraminator is not configured.
    """
    del ctx
    async with metrics.job("reconcile_tetraminator_payments"):
        from cloud_platform.core.config import get_settings
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
        from cloud_platform.modules.payments.reconcile import reconcile_tetraminator_pending
        from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
        from cloud_platform.modules.payments.service import PaymentWebhookService
        from cloud_platform.modules.wallet.repository import (
            SqlAlchemyLedgerRepository,
            SqlAlchemyWalletRepository,
        )
        from cloud_platform.providers.tetraminator.client import TetraminatorGateway

        settings = get_settings()
        if not settings.tetraminator_enabled or not settings.tetraminator_api_key:
            return
        gateway = TetraminatorGateway(
            api_key=settings.tetraminator_api_key,
            base_url=settings.tetraminator_base_url,
            callback_url=settings.tetraminator_callback_url,
            timeout_seconds=settings.tetraminator_timeout_seconds,
            require_https_callback=(settings.app_env or "").strip().lower() == "production",
        )
        try:
            report = await reconcile_tetraminator_pending(
                payments_repo=SqlAlchemyPaymentSessionRepository(SessionFactory),
                webhook_service=PaymentWebhookService(
                    payments_repo=SqlAlchemyPaymentSessionRepository(SessionFactory),
                    wallet_repo=SqlAlchemyWalletRepository(SessionFactory),
                    ledger_repo=SqlAlchemyLedgerRepository(SessionFactory),
                ),
                gateway=gateway,
                audit_repo=SqlAlchemyAuditRepository(SessionFactory),
            )
            logger.info("tetraminator reconcile: %s", report.render())
        finally:
            await gateway.close()


async def deliver_business_log_events(ctx: dict[str, object]) -> None:
    """Deliver queued operator-channel business events (release hardening).

    The ONLY place a business event reaches Telegram. Events were enqueued
    durably by the application services, so this job can fail, retry or lag
    without ever affecting checkout, settlement, ordering or reconciliation.
    Claiming is atomic and retries are bounded, so a re-run cannot flood the
    channel. Skipped entirely when the logger channel is not configured.
    """
    del ctx
    async with metrics.job("deliver_business_log_events"):
        from cloud_platform.core.config import get_settings

        settings = get_settings()
        if not settings.telegram_logger_enabled or not settings.telegram_logger_chat_id:
            return
        token = _telegram_bot_token()
        if token is None:
            return
        from aiogram import Bot

        from cloud_platform.core.container import create_container

        bot = Bot(token=token)
        container = create_container()
        try:
            dispatcher = container.business_log_dispatcher(bot)
            report = await dispatcher.deliver()
            logger.info(
                "business log delivery: sent=%s retried=%s abandoned=%s",
                report.sent,
                report.retried,
                report.abandoned,
            )
        finally:
            await container.close()
            await bot.session.close()


def _cron_jobs() -> list[Any]:
    """Cron schedule for the LEASEWEB-MVP jobs (periodic catalog discovery,
    order worker/reconciler, daily renewal).     Overlapping runs are safe: the
    order worker claims through the operation ledger and the reconciler
    never mutates. The catalog auto-sync runs at the configured interval
    (default 15 minutes) so account eligibility changes surface without
    operator action, and carries its own dedicated job timeout
    (:func:`catalog_auto_sync_timeout`) because a complete provider walk does
    not fit the generic worker job timeout."""
    from arq.cron import cron

    every_minute = set(range(0, 60))
    every_two_minutes = set(range(0, 60, 2))
    every_three_minutes = set(range(0, 60, 3))
    every_fifteen_minutes = set(range(0, 60, 15))
    # The catalog refresh is the only schedule with an explicit timeout: a
    # full provider walk (hundreds of products x accounts x regions) does not
    # fit the generic worker job timeout, while every other job deliberately
    # keeps it.
    catalog_timeout = catalog_auto_sync_timeout()
    return [
        cron(
            catalog_auto_sync,
            minute=catalog_auto_sync_minutes(),
            run_at_startup=True,
            timeout=catalog_timeout,
        ),
        cron(process_cloud_creates, minute=every_two_minutes, run_at_startup=True),
        cron(reconcile_cloud_creates, minute=every_three_minutes, run_at_startup=True),
        cron(reconcile_provider_resources, minute=every_three_minutes, run_at_startup=True),
        cron(process_deletes, minute=every_two_minutes, run_at_startup=True),
        cron(reconcile_deletes, minute=every_three_minutes, run_at_startup=True),
        cron(accrue_usage, minute={0}, run_at_startup=True),
        cron(evaluate_low_balance, minute=every_fifteen_minutes, run_at_startup=True),
        cron(process_leaseweb_orders, minute=every_two_minutes, run_at_startup=True),
        cron(reconcile_leaseweb_orders, minute=every_three_minutes, run_at_startup=True),
        cron(check_renewals, hour={3}, minute={23}, run_at_startup=True),
        cron(deliver_business_log_events, minute=every_minute, run_at_startup=True),
        cron(reconcile_tetraminator_payments, minute=every_fifteen_minutes, run_at_startup=True),
        cron(reconcile_payments, minute=every_fifteen_minutes, run_at_startup=True),
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
        catalog_auto_sync,
        process_cloud_creates,
        reconcile_cloud_creates,
        process_leaseweb_orders,
        reconcile_leaseweb_orders,
        reconcile_tetraminator_payments,
        check_renewals,
        deliver_business_log_events,
    ]
    cron_jobs: ClassVar[list[Any]] = _cron_jobs()
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 20
    # Generic per-job timeout. The catalog refresh is the one exception: its
    # cron entry sets an explicit, larger timeout (a full provider walk would
    # otherwise be cancelled mid-run).
    job_timeout = 120


# M16-004: queue partitioning by responsibility. Each role runs as its own
# arq worker process with an isolated queue so a billing backlog can never
# starve provisioning (and vice versa). Enqueue with
# ``await redis.enqueue_job("accrue_usage", _queue_name="billing")`` etc.
PROVISIONING_FUNCTIONS: list[Any] = [
    reconcile_provider_resources,
    process_deletes,
    reconcile_deletes,
    catalog_auto_sync,
    process_cloud_creates,
    reconcile_cloud_creates,
    process_leaseweb_orders,
    reconcile_leaseweb_orders,
]
BILLING_FUNCTIONS: list[Any] = [
    accrue_usage,
    evaluate_low_balance,
    reconcile_payments,
    reconcile_tetraminator_payments,
    check_renewals,
    deliver_business_log_events,
]


def _role_cron_jobs(role: str) -> list[Any]:
    """Build only the schedules owned by one isolated worker role."""
    from arq.cron import cron

    every_minute = set(range(0, 60))
    every_two_minutes = set(range(0, 60, 2))
    every_three_minutes = set(range(0, 60, 3))
    every_fifteen_minutes = set(range(0, 60, 15))
    if role == "provisioning":
        return [
            cron(
                catalog_auto_sync,
                minute=catalog_auto_sync_minutes(),
                run_at_startup=True,
                timeout=catalog_auto_sync_timeout(),
            ),
            cron(process_cloud_creates, minute=every_two_minutes, run_at_startup=True),
            cron(reconcile_cloud_creates, minute=every_three_minutes, run_at_startup=True),
            cron(reconcile_provider_resources, minute=every_three_minutes, run_at_startup=True),
            cron(process_leaseweb_orders, minute=every_two_minutes, run_at_startup=True),
            cron(reconcile_leaseweb_orders, minute=every_three_minutes, run_at_startup=True),
            cron(process_deletes, minute=every_two_minutes, run_at_startup=True),
            cron(reconcile_deletes, minute=every_three_minutes, run_at_startup=True),
        ]
    if role == "billing":
        return [
            cron(accrue_usage, minute={0}, run_at_startup=True),
            cron(evaluate_low_balance, minute=every_fifteen_minutes, run_at_startup=True),
            cron(reconcile_payments, minute=every_fifteen_minutes, run_at_startup=True),
            cron(
                reconcile_tetraminator_payments, minute=every_fifteen_minutes, run_at_startup=True
            ),
            cron(check_renewals, hour={3}, minute={23}, run_at_startup=True),
        ]
    return [
        cron(deliver_business_log_events, minute=every_minute, run_at_startup=True),
        cron(evaluate_low_balance, minute=every_fifteen_minutes, run_at_startup=True),
        cron(check_renewals, hour={3}, minute={23}, run_at_startup=True),
    ]


class ProvisioningWorkerSettings(WorkerSettings):
    """Provisioning queue: creates, deletes, provider reconciliation."""

    queue_name = "provisioning"
    functions: ClassVar[list[Any]] = PROVISIONING_FUNCTIONS
    cron_jobs: ClassVar[list[Any]] = _role_cron_jobs("provisioning")
    max_jobs = 10


class BillingWorkerSettings(WorkerSettings):
    """Billing queue: accrual, low-balance policy, payment reconciliation."""

    queue_name = "billing"
    functions: ClassVar[list[Any]] = BILLING_FUNCTIONS
    cron_jobs: ClassVar[list[Any]] = _role_cron_jobs("billing")
    max_jobs = 10


class NotifyWorkerSettings(WorkerSettings):
    """Notify queue: Telegram/low-balance notifications (same functions, own queue)."""

    queue_name = "notify"
    functions: ClassVar[list[Any]] = [
        deliver_business_log_events,
        evaluate_low_balance,
        check_renewals,
    ]
    cron_jobs: ClassVar[list[Any]] = _role_cron_jobs("notify")
    max_jobs = 20


WORKER_QUEUES: tuple[str, ...] = ("provisioning", "billing", "notify")
