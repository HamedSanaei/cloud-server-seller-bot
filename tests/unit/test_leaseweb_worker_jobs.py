"""Leaseweb worker job wiring tests (LEASEWEB-MVP).

Each cron job is exercised with a fake container / provider / syncer so the
job body (settings gates, construction, outcome logging, recovery pass)
is covered without a database or network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_platform.worker.settings as ws
from cloud_platform.core.config import Settings


def _settings(**overrides: Any) -> Settings:
    # A REAL Settings object: db/session.py builds its engine at import time
    # from get_settings(), so a MagicMock URL would crash module imports.
    values: dict[str, Any] = dict(
        leaseweb_api_key="k",
        leaseweb_api_base_url="https://api.test",
        leaseweb_locations="AMS-01,FRA-01",
        leaseweb_os_allowlist="",
        leaseweb_order_os_only_free=True,
        telegram_bot_token="",
        telegram_admin_chat_id=0,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake = _settings()
    monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: fake)
    return fake


def _fake_container() -> MagicMock:
    container = MagicMock()
    container.initialize = AsyncMock()
    container.close = AsyncMock()
    container.order_worker = MagicMock(
        return_value=AsyncMock(process_pending=AsyncMock(return_value={"submitted": 1}))
    )
    container.order_reconciler = MagicMock(
        return_value=AsyncMock(reconcile=AsyncMock(return_value={"still_provisioning": 1}))
    )
    container.order_recovery = MagicMock(
        return_value=AsyncMock(recover=AsyncMock(return_value={"recovered": 1}))
    )
    container.renewal_checker = MagicMock(return_value=AsyncMock(run=AsyncMock(return_value=[])))
    return container


class TestSyncLeasewebOffers:
    async def test_skipped_without_key(self, settings: MagicMock) -> None:
        # Fully uncredentialed: blanking the key must also drop the
        # validator-synthesized default account, otherwise the job builds a
        # real router and reaches the network instead of skipping.
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        await ws.sync_leaseweb_offers({})

    async def test_runs_syncer_and_logs(
        self, settings: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        step = MagicMock(total_fetched=5, total_upserted=3, errors=[])
        syncer = AsyncMock(sync_all=AsyncMock(return_value={"ams": step}))
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.LeaseWebOrderingCatalogSyncer",
            lambda *a, **k: syncer,
        )
        await ws.sync_leaseweb_offers({})
        syncer.sync_all.assert_awaited_once()


class TestProcessLeasewebOrders:
    async def test_skipped_without_key(self, settings: MagicMock) -> None:
        # Fully uncredentialed: blanking the key must also drop the
        # validator-synthesized default account, mirroring a fresh config
        # load without any credential.
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        await ws.process_leaseweb_orders({})

    async def test_runs_worker(self, settings: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        container = _fake_container()
        import cloud_platform.core.container as _c

        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        assert _c.create_container() is container  # sanity: patch is live
        await ws.process_leaseweb_orders({})
        container.initialize.assert_awaited_once()
        container.order_worker.assert_called_once_with(delivery_notifier=None)
        container.order_worker().process_pending.assert_awaited_once_with(limit=10)
        container.close.assert_awaited_once()

    async def test_with_telegram_notifiers(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.telegram_bot_token = "t"
        container = _fake_container()
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        notifier = MagicMock(deliver=AsyncMock())
        monkeypatch.setattr(
            "cloud_platform.worker.settings._telegram_notifiers",
            lambda: (notifier, MagicMock()),
        )
        await ws.process_leaseweb_orders({})
        assert container.order_worker.call_args.kwargs["delivery_notifier"] is notifier


class TestReconcileLeasewebOrders:
    async def test_skipped_without_key(self, settings: MagicMock) -> None:
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        await ws.reconcile_leaseweb_orders({})

    async def test_runs_reconciler_then_read_only_recovery(
        self, settings: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        container = _fake_container()
        container.provider_registry = MagicMock()
        container.provider_registry.keys = MagicMock(return_value=["leaseweb"])
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        await ws.reconcile_leaseweb_orders({})
        container.order_reconciler().reconcile.assert_awaited_once_with(
            provider_key="leaseweb", limit=100
        )
        # The recovery pass is part of the SAME job and NEVER POSTs.
        container.order_recovery().recover.assert_awaited_once_with(
            provider_key="leaseweb", limit=50
        )


class TestCheckRenewals:
    async def test_runs_checker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        container = _fake_container()
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        await ws.check_renewals({})
        container.renewal_checker.assert_called_once()
        container.renewal_checker().run.assert_awaited_once()
        container.close.assert_awaited_once()


class TestCronWiring:
    def test_cron_jobs_include_all_leaseweb_jobs(self) -> None:
        jobs = ws._cron_jobs()
        names = {getattr(job, "coroutine", getattr(job, "func", None)).__name__ for job in jobs}
        assert {
            "catalog_auto_sync",
            "process_leaseweb_orders",
            "reconcile_leaseweb_orders",
            "check_renewals",
            "reconcile_tetraminator_payments",
        } <= names

    def test_worker_settings_functions_include_leaseweb_jobs(self) -> None:
        names = {f.__name__ for f in ws.WorkerSettings.functions}
        assert "sync_leaseweb_offers" in names
        assert "process_leaseweb_orders" in names
        assert "reconcile_leaseweb_orders" in names
        assert "check_renewals" in names
        assert "reconcile_tetraminator_payments" in names


class TestWorkerLifecycleAndNotifiers:
    async def test_startup_sets_service_and_tracing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        setup = MagicMock()
        monkeypatch.setattr("cloud_platform.observability.tracing.setup_tracing", setup)
        ctx: dict[str, object] = {}
        await ws.startup(ctx)
        assert ctx["service"] == "cloud-platform-worker"
        assert setup.called

    async def test_shutdown_clears_context(self) -> None:
        ctx: dict[str, object] = {"service": "x"}
        await ws.shutdown(ctx)
        assert ctx == {}

    def test_telegram_notifiers_none_without_token(self, settings: MagicMock) -> None:
        settings.telegram_bot_token = ""
        assert ws._telegram_notifiers() is None

    def test_telegram_notifiers_built_with_token(
        self, settings: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.telegram_bot_token = "123456:ok"
        settings.telegram_admin_chat_id = 42
        delivery = MagicMock()
        renewal = MagicMock()
        bot = MagicMock()
        monkeypatch.setattr("aiogram.Bot", lambda token: bot)
        monkeypatch.setattr(
            "cloud_platform.bot.notifier.TelegramOrderDeliveryNotifier",
            lambda b, u: delivery,
        )
        monkeypatch.setattr(
            "cloud_platform.bot.notifier.TelegramRenewalNotifier",
            lambda b, u, a: renewal,
        )
        result = ws._telegram_notifiers()
        assert result == (delivery, renewal)

    def test_cron_schedule_details(self) -> None:
        jobs = ws._cron_jobs()
        by_name = {
            getattr(job, "coroutine", getattr(job, "func", None)).__name__: job for job in jobs
        }
        assert len(by_name) == 15
        assert by_name["process_leaseweb_orders"].minute == set(range(0, 60, 2))
        assert by_name["reconcile_leaseweb_orders"].minute == set(range(0, 60, 3))
        # Catalog auto-sync runs at the configured interval (15 min default).
        assert by_name["catalog_auto_sync"].minute == set(range(0, 60, 15))
        # Automatic capacity recovery is read-only and runs at its own cadence
        # (3 minutes by default), independent of the catalog walk.
        assert by_name["reconcile_cloud_capacity"].minute == set(range(0, 60, 3))
        # Hourly cloud submission uses the order-worker cadence.
        assert by_name["process_cloud_creates"].minute == set(range(0, 60, 2))
        assert by_name["reconcile_cloud_creates"].minute == set(range(0, 60, 3))
        # Hourly usage settlement runs once per hour.
        assert by_name["accrue_usage"].minute == {0}
        assert by_name["check_renewals"].hour == {3}
        assert by_name["check_renewals"].minute == {23}
        # Tetraminator pending-payment reconciliation is bounded polling.
        assert by_name["reconcile_tetraminator_payments"].minute == set(range(0, 60, 15))
        # The business-log delivery pass runs every minute (bounded retries).
        assert by_name["deliver_business_log_events"].minute == set(range(0, 60))
        # Lifecycle/payment jobs: low-balance policy, delete saga + recovery,
        # stuck-payment recheck, and the provider-resource reconciler.
        assert by_name["evaluate_low_balance"].minute == set(range(0, 60, 15))
        assert by_name["process_deletes"].minute == set(range(0, 60, 2))
        assert by_name["reconcile_deletes"].minute == set(range(0, 60, 3))
        assert by_name["reconcile_payments"].minute == set(range(0, 60, 15))
        assert by_name["reconcile_provider_resources"].minute == set(range(0, 60, 3))
        for job in jobs:
            assert getattr(job, "run_at_startup", False) is True

    def test_worker_settings_cron_jobs_match(self) -> None:
        assert len(ws.WorkerSettings.cron_jobs) == 15
        assert ws.WorkerSettings.on_startup is ws.startup
        assert ws.WorkerSettings.on_shutdown is ws.shutdown


class TestAccrueUsage:
    async def test_runs_accrual_job_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}
        fake_job = MagicMock(run=AsyncMock(return_value=MagicMock()))

        def _factory(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return fake_job

        monkeypatch.setattr("cloud_platform.modules.billing.service.AccrualJob", _factory)
        await ws.accrue_usage({})
        fake_job.run.assert_awaited_once_with()
        assert {"server_repo", "wallet_repo", "accrual_repo"} <= set(captured)

    async def test_failure_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_job = MagicMock(run=AsyncMock(side_effect=RuntimeError("db down")))
        monkeypatch.setattr(
            "cloud_platform.modules.billing.service.AccrualJob",
            lambda **kwargs: fake_job,
        )
        with pytest.raises(RuntimeError):
            await ws.accrue_usage({})


class TestEvaluateLowBalance:
    async def test_runs_policy_with_config_from_settings(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.low_balance_threshold_minor = 7000
        settings.low_balance_grace_hours = 5
        captured: dict[str, Any] = {}
        fake_service = MagicMock(evaluate=AsyncMock(return_value=MagicMock()))

        async def _evaluate(config: Any, *a: Any, **k: Any) -> Any:
            captured["config"] = config
            return MagicMock()

        fake_service.evaluate = AsyncMock(side_effect=_evaluate)
        monkeypatch.setattr(
            "cloud_platform.modules.billing.service.LowBalancePolicyService",
            lambda *a, **k: fake_service,
        )
        await ws.evaluate_low_balance({})
        assert fake_service.evaluate.await_count == 1
        config = captured["config"]
        assert config.threshold_minor == 7000
        assert config.grace_hours == 5

    async def test_failure_propagates(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_service = MagicMock(evaluate=AsyncMock(side_effect=RuntimeError("policy down")))
        monkeypatch.setattr(
            "cloud_platform.modules.billing.service.LowBalancePolicyService",
            lambda *a, **k: fake_service,
        )
        with pytest.raises(RuntimeError):
            await ws.evaluate_low_balance({})


class TestProcessDeletes:
    def _patch_deletes(
        self, monkeypatch: pytest.MonkeyPatch, *, worker_result: Any = None
    ) -> tuple[Any, Any]:
        fake_worker = MagicMock(
            process_pending_deletes=AsyncMock(return_value=dict(worker_result or {"executed": 1}))
        )
        monkeypatch.setattr(
            "cloud_platform.modules.operations.service.DeleteWorker",
            lambda *a, **k: fake_worker,
        )
        monkeypatch.setattr(
            "cloud_platform.modules.operations.service.DeleteOperationExecutor",
            MagicMock,
        )
        monkeypatch.setattr("cloud_platform.modules.billing.service.FinalChargeService", MagicMock)
        fake_ops = MagicMock(oldest_pending_age_seconds=AsyncMock(return_value=None))
        monkeypatch.setattr(
            "cloud_platform.modules.operations.repository.SqlAlchemyOperationRepository",
            lambda *a, **k: fake_ops,
        )
        return fake_worker, fake_ops

    async def test_runs_worker_with_empty_registry(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.hetzner_api_token = ""
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            lambda s: None,
        )
        fake_registry = MagicMock()
        monkeypatch.setattr(
            "cloud_platform.providers.registry.ProviderRegistry",
            lambda: fake_registry,
        )
        fake_worker, fake_ops = self._patch_deletes(monkeypatch)
        await ws.process_deletes({})
        fake_worker.process_pending_deletes.assert_awaited_once_with()
        fake_ops.oldest_pending_age_seconds.assert_awaited_once()

    async def test_hetzner_provider_registered_and_closed(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.hetzner_api_token = "h-token"
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            lambda s: None,
        )
        fake_registry = MagicMock()
        monkeypatch.setattr(
            "cloud_platform.providers.registry.ProviderRegistry",
            lambda: fake_registry,
        )
        fake_provider = MagicMock(aclose=AsyncMock())
        monkeypatch.setattr(
            "cloud_platform.providers.hetzner.client.HetznerCloudProvider",
            lambda **k: fake_provider,
        )
        fake_worker, _ = self._patch_deletes(monkeypatch)
        await ws.process_deletes({})
        fake_registry.register.assert_called_once_with(fake_provider)
        fake_worker.process_pending_deletes.assert_awaited_once()
        fake_provider.aclose.assert_awaited_once()


class TestReconcileDeletes:
    async def test_runs_reconciler_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = MagicMock(reconcile=AsyncMock(return_value={}))
        monkeypatch.setattr(
            "cloud_platform.modules.operations.service.DeleteTimeoutReconciler",
            lambda *a, **k: fake,
        )
        await ws.reconcile_deletes({})
        fake.reconcile.assert_awaited_once_with()


class TestReconcilePayments:
    async def test_skipped_without_gateway(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.zarinpal_merchant_id = ""

        def _fail(*a: Any, **k: Any) -> Any:
            raise AssertionError("gateway must not be constructed")

        monkeypatch.setattr("cloud_platform.providers.zarinpal.client.ZarinPalGateway", _fail)
        await ws.reconcile_payments({})

    async def test_runs_service_when_configured(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.zarinpal_merchant_id = "merchant-1"
        fake_gateway = MagicMock(close=AsyncMock())
        monkeypatch.setattr(
            "cloud_platform.providers.zarinpal.client.ZarinPalGateway",
            lambda **k: fake_gateway,
        )
        fake_service = MagicMock(run=AsyncMock(return_value=MagicMock()))
        monkeypatch.setattr(
            "cloud_platform.modules.payments.reconcile.PaymentReconciliationService",
            lambda *a, **k: fake_service,
        )
        await ws.reconcile_payments({})
        fake_service.run.assert_awaited_once_with()
        fake_gateway.close.assert_awaited_once()


class TestReconcileTetraminator:
    async def test_skipped_when_disabled(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.tetraminator_enabled = False
        settings.tetraminator_api_key = ""

        def _fail(*a: Any, **k: Any) -> Any:
            raise AssertionError("gateway must not be constructed")

        monkeypatch.setattr(
            "cloud_platform.providers.tetraminator.client.TetraminatorGateway", _fail
        )
        await ws.reconcile_tetraminator_payments({})

    async def test_runs_when_configured(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.tetraminator_enabled = True
        settings.tetraminator_api_key = "tetra-key"  # pragma: allowlist secret
        fake_gateway = MagicMock(close=AsyncMock())
        monkeypatch.setattr(
            "cloud_platform.providers.tetraminator.client.TetraminatorGateway",
            lambda **k: fake_gateway,
        )
        fake_report = MagicMock(render=MagicMock(return_value="checked=1"))
        monkeypatch.setattr(
            "cloud_platform.modules.payments.reconcile.reconcile_tetraminator_pending",
            AsyncMock(return_value=fake_report),
        )
        await ws.reconcile_tetraminator_payments({})
        fake_gateway.close.assert_awaited_once()


class TestSyncLeasewebOffersBranches:
    async def test_single_provider_branch(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.leaseweb_api_key = "k-single"  # pragma: allowlist secret
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            lambda s: None,
        )
        fake_provider = MagicMock()
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.ordering_provider_from_settings",
            lambda s: fake_provider,
        )
        step = MagicMock(total_fetched=2, total_upserted=1, errors=[])
        syncer = AsyncMock(sync_all=AsyncMock(return_value={"ams": step}))
        captured: dict[str, Any] = {}

        def _factory(session_factory: Any, provider: Any) -> Any:
            captured["provider"] = provider
            return syncer

        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.LeaseWebOrderingCatalogSyncer",
            _factory,
        )
        closed: list[Any] = []
        monkeypatch.setattr(
            "cloud_platform.worker.settings._close_owned_resources",
            AsyncMock(side_effect=lambda r: closed.extend(r)),
        )
        await ws.sync_leaseweb_offers({})
        assert captured["provider"] is fake_provider
        syncer.sync_all.assert_awaited_once()
        assert closed == [fake_provider]

    async def test_router_branch_passes_accounts(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_router = MagicMock()
        fake_router.ordered_providers = {"a": MagicMock()}
        fake_router.priorities = {"a": 1}
        fake_router.account_states = {"a": MagicMock()}
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            lambda s: fake_router,
        )
        step = MagicMock(total_fetched=1, total_upserted=1, errors=[])
        syncer = AsyncMock(sync_all=AsyncMock(return_value={"a": step}))
        captured: dict[str, Any] = {}

        def _factory(session_factory: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return syncer

        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.LeaseWebOrderingCatalogSyncer",
            _factory,
        )
        await ws.sync_leaseweb_offers({})
        assert captured["accounts"] == fake_router.ordered_providers
        syncer.sync_all.assert_awaited_once()
