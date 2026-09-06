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
        settings.leaseweb_api_key = ""
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
        settings.leaseweb_api_key = ""
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
        await ws.reconcile_leaseweb_orders({})

    async def test_runs_reconciler_then_read_only_recovery(
        self, settings: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        container = _fake_container()
        monkeypatch.setattr("cloud_platform.core.container.create_container", lambda: container)
        await ws.reconcile_leaseweb_orders({})
        container.order_reconciler().reconcile.assert_awaited_once_with(limit=100)
        # The recovery pass is part of the SAME job and NEVER POSTs.
        container.order_recovery().recover.assert_awaited_once_with(limit=50)


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
            "sync_leaseweb_offers",
            "process_leaseweb_orders",
            "reconcile_leaseweb_orders",
            "check_renewals",
        } <= names

    def test_worker_settings_functions_include_leaseweb_jobs(self) -> None:
        names = {f.__name__ for f in ws.WorkerSettings.functions}
        assert "sync_leaseweb_offers" in names
        assert "process_leaseweb_orders" in names
        assert "reconcile_leaseweb_orders" in names
        assert "check_renewals" in names


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
        settings.telegram_bot_token = "t:ok"
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
        assert len(by_name) == 4
        assert by_name["process_leaseweb_orders"].minute == set(range(0, 60, 2))
        assert by_name["reconcile_leaseweb_orders"].minute == set(range(0, 60, 3))
        assert by_name["sync_leaseweb_offers"].hour == {3}
        assert by_name["check_renewals"].hour == {3}
        for job in jobs:
            assert getattr(job, "run_at_startup", False) is True

    def test_worker_settings_cron_jobs_match(self) -> None:
        assert len(ws.WorkerSettings.cron_jobs) == 4
        assert ws.WorkerSettings.on_startup is ws.startup
        assert ws.WorkerSettings.on_shutdown is ws.shutdown
