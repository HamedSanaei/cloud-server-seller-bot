"""Worker credential gates, failure propagation, and resource cleanup."""

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
        hetzner_api_token="",
        hetzner_accounts=[],
        arvancloud_api_key="",
        telegram_bot_token="",
        telegram_admin_chat_id=0,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    fake = _settings()
    monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: fake)
    monkeypatch.setattr(
        "cloud_platform.db.session.SessionFactory",
        MagicMock(side_effect=AssertionError("unit test must not access the database")),
    )
    return fake


class TestSyncLeasewebOffers:
    async def test_skipped_without_key(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fully uncredentialed: blanking the key must also drop the
        # validator-synthesized default account, otherwise the job builds a
        # real router and reaches the network instead of skipping.
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        syncer_factory = MagicMock(side_effect=AssertionError("uncredentialed catalog sync"))
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.ordering_sync.LeaseWebOrderingCatalogSyncer",
            syncer_factory,
        )
        await ws.sync_leaseweb_offers({})
        syncer_factory.assert_not_called()


class TestProcessLeasewebOrders:
    async def test_skipped_without_key(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fully uncredentialed: blanking the key must also drop the
        # validator-synthesized default account, mirroring a fresh config
        # load without any credential.
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        container_factory = MagicMock(side_effect=AssertionError("uncredentialed submission"))
        monkeypatch.setattr("cloud_platform.core.container.create_container", container_factory)
        await ws.process_leaseweb_orders({})
        container_factory.assert_not_called()


class TestReconcileLeasewebOrders:
    async def test_skipped_without_key(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        container_factory = MagicMock(side_effect=AssertionError("uncredentialed reconciliation"))
        monkeypatch.setattr("cloud_platform.core.container.create_container", container_factory)
        await ws.reconcile_leaseweb_orders({})
        container_factory.assert_not_called()


class TestTelegramNotifiers:
    def test_telegram_notifiers_none_without_token(self, settings: Settings) -> None:
        settings.telegram_bot_token = ""
        assert ws._telegram_notifiers() is None


class TestAccrueUsage:
    async def test_failure_propagates(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_job = MagicMock(run=AsyncMock(side_effect=RuntimeError("db down")))
        monkeypatch.setattr(
            "cloud_platform.modules.billing.service.AccrualJob",
            lambda **kwargs: fake_job,
        )
        with pytest.raises(RuntimeError, match="db down"):
            await ws.accrue_usage({})


class TestEvaluateLowBalance:
    async def test_failure_propagates(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_service = MagicMock(evaluate=AsyncMock(side_effect=RuntimeError("policy down")))
        monkeypatch.setattr(
            "cloud_platform.modules.billing.service.LowBalancePolicyService",
            lambda *a, **k: fake_service,
        )
        with pytest.raises(RuntimeError, match="policy down"):
            await ws.evaluate_low_balance({})


class TestProcessDeletes:
    @pytest.mark.parametrize("close_fails", [False, True], ids=["closed", "close-error"])
    async def test_routers_closed_after_delete_failure(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        close_fails: bool,
    ) -> None:
        hetzner_router = MagicMock(
            providers={},
            views=MagicMock(return_value=()),
            aclose=AsyncMock(),
        )
        leaseweb_router = MagicMock(
            ordered_providers={},
            views=MagicMock(return_value=()),
            new_order_clients=MagicMock(return_value=[]),
            aclose=AsyncMock(side_effect=RuntimeError("close down") if close_fails else None),
        )
        monkeypatch.setattr(
            "cloud_platform.providers.hetzner.accounts.build_hetzner_account_router",
            lambda settings: hetzner_router,
        )
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            lambda settings: leaseweb_router,
        )
        failure = RuntimeError("delete down")
        worker = MagicMock(process_pending_deletes=AsyncMock(side_effect=failure))
        monkeypatch.setattr(
            "cloud_platform.modules.operations.service.DeleteWorker",
            lambda *args, **kwargs: worker,
        )
        monkeypatch.setattr(
            "cloud_platform.modules.operations.service.DeleteOperationExecutor",
            MagicMock,
        )
        monkeypatch.setattr("cloud_platform.modules.billing.service.FinalChargeService", MagicMock)

        with pytest.raises(RuntimeError, match="delete down") as raised:
            await ws.process_deletes({})

        assert raised.value is failure
        leaseweb_router.aclose.assert_awaited_once()
        hetzner_router.aclose.assert_awaited_once()


class TestReconcilePayments:
    async def test_skipped_without_gateway(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.zarinpal_merchant_id = ""

        def _fail(*a: Any, **k: Any) -> Any:
            raise AssertionError("gateway must not be constructed")

        monkeypatch.setattr("cloud_platform.providers.zarinpal.client.ZarinPalGateway", _fail)
        await ws.reconcile_payments({})

    async def test_gateway_closed_after_reconciliation_failure(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.zarinpal_merchant_id = "merchant-1"
        fake_gateway = MagicMock(close=AsyncMock())
        monkeypatch.setattr(
            "cloud_platform.providers.zarinpal.client.ZarinPalGateway",
            lambda **k: fake_gateway,
        )
        failure = RuntimeError("gateway down")
        fake_service = MagicMock(run=AsyncMock(side_effect=failure))
        monkeypatch.setattr(
            "cloud_platform.modules.payments.reconcile.PaymentReconciliationService",
            lambda *a, **k: fake_service,
        )
        with pytest.raises(RuntimeError, match="gateway down") as raised:
            await ws.reconcile_payments({})
        assert raised.value is failure
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

    async def test_gateway_closed_after_reconciliation_failure(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.tetraminator_enabled = True
        settings.tetraminator_api_key = "tetra-key"  # pragma: allowlist secret
        fake_gateway = MagicMock(close=AsyncMock())
        monkeypatch.setattr(
            "cloud_platform.providers.tetraminator.client.TetraminatorGateway",
            lambda **k: fake_gateway,
        )
        monkeypatch.setattr(
            "cloud_platform.modules.payments.reconcile.reconcile_tetraminator_pending",
            AsyncMock(side_effect=RuntimeError("inquiry down")),
        )
        with pytest.raises(RuntimeError, match="inquiry down"):
            await ws.reconcile_tetraminator_payments({})
        fake_gateway.close.assert_awaited_once()
