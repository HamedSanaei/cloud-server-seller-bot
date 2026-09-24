"""Worker wiring for the automatic catalog refresh (STOREFRONT-V2).

- The job is skipped when disabled by configuration or when no provider is
  configured (a missing Hetzner credential never breaks the Leaseweb sync).
- An operator-disabled provider is skipped; per-provider failures stay
  isolated inside the coordinator run.
- The cron cadence follows the configured interval (15-minute default).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_platform.worker.settings as ws
from cloud_platform.core.config import Settings
from cloud_platform.modules.offers.auto_sync import AutoSyncRunReport
from cloud_platform.modules.offers.domain import CatalogSyncState


def _settings(**overrides: Any) -> Settings:
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


def _coordinator(
    monkeypatch: pytest.MonkeyPatch, report: AutoSyncRunReport | None = None
) -> AsyncMock:
    coordinator = AsyncMock()
    coordinator.run = AsyncMock(
        return_value=report if report is not None else AutoSyncRunReport(ran=True, providers=())
    )
    monkeypatch.setattr(
        "cloud_platform.modules.offers.auto_sync.CatalogAutoSyncCoordinator",
        lambda **kwargs: coordinator,
    )
    return coordinator


class TestCatalogAutoSyncJob:
    async def test_skipped_when_disabled(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.storefront_catalog_sync_enabled = False
        coordinator = _coordinator(monkeypatch)
        await ws.catalog_auto_sync({})
        coordinator.run.assert_not_awaited()

    async def test_runs_leaseweb_without_hetzner_credential(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        settings.hetzner_api_token = ""
        coordinator = _coordinator(monkeypatch)
        with caplog.at_level("INFO", logger="cloud_platform.worker.settings"):
            await ws.catalog_auto_sync({})
        coordinator.run.assert_awaited_once()

    async def test_runs_both_providers_when_configured(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.hetzner_api_token = "hetzner-token"
        seen: dict[str, Any] = {}

        def _factory(**kwargs: Any) -> AsyncMock:
            seen.update(kwargs)
            coordinator = AsyncMock()
            coordinator.run = AsyncMock(return_value=AutoSyncRunReport(ran=True, providers=()))
            return coordinator

        monkeypatch.setattr(
            "cloud_platform.modules.offers.auto_sync.CatalogAutoSyncCoordinator", _factory
        )
        await ws.catalog_auto_sync({})
        assert {source.provider_key for source in seen["sources"]} == {"leaseweb", "hetzner"}

    async def test_operator_disabled_provider_is_skipped(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.hetzner_api_token = "hetzner-token"
        settings.providers_enabled = {"hetzner": False}
        seen: dict[str, Any] = {}

        def _factory(**kwargs: Any) -> AsyncMock:
            seen.update(kwargs)
            coordinator = AsyncMock()
            coordinator.run = AsyncMock(return_value=AutoSyncRunReport(ran=True, providers=()))
            return coordinator

        monkeypatch.setattr(
            "cloud_platform.modules.offers.auto_sync.CatalogAutoSyncCoordinator", _factory
        )
        await ws.catalog_auto_sync({})
        assert {source.provider_key for source in seen["sources"]} == {"leaseweb"}

    async def test_no_providers_configured_does_nothing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        settings.hetzner_api_token = ""
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            lambda settings: None,
        )
        coordinator = _coordinator(monkeypatch)
        await ws.catalog_auto_sync({})
        coordinator.run.assert_not_awaited()

    async def test_skipped_run_is_logged(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        coordinator = _coordinator(monkeypatch, AutoSyncRunReport(ran=False, reason="lock held"))
        with caplog.at_level("INFO", logger="cloud_platform.worker.settings"):
            await ws.catalog_auto_sync({})
        coordinator.run.assert_awaited_once()
        assert any("lock held" in record.message for record in caplog.records)


class TestCatalogAutoSyncSchedule:
    def test_default_interval_is_fifteen_minutes(self) -> None:
        assert ws.catalog_auto_sync_minutes() == set(range(0, 60, 15))

    def test_custom_hour_dividing_interval(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.storefront_catalog_sync_interval_seconds = 600
        assert ws.catalog_auto_sync_minutes() == set(range(0, 60, 10))

    def test_non_dividing_interval_falls_back(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.storefront_catalog_sync_interval_seconds = 700
        assert ws.catalog_auto_sync_minutes() == set(range(0, 60, 15))

    def test_cron_uses_the_configured_cadence(self) -> None:
        jobs = ws._cron_jobs()
        by_name = {
            getattr(job, "coroutine", getattr(job, "func", None)).__name__: job for job in jobs
        }
        assert by_name["catalog_auto_sync"].minute == set(range(0, 60, 15))
        assert getattr(by_name["catalog_auto_sync"], "run_at_startup", False) is True


class _DeterministicEurUsdRates:
    """Fake EUR/USD reference rates (no network): 1.17, frankfurter family."""

    async def get_rate(self, base: str, quote: str, *, allow_catalog_stale: bool = False) -> Any:
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from cloud_platform.modules.fx.domain import FxReferenceQuote
        from cloud_platform.modules.fx.service import ReferenceRateResolution

        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=Decimal("1.17"),
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )


async def _usd_priced_offer(cost_minor: int = 499) -> Any:
    """Production-shaped monthly EUR offer, priced to USD in-test.

    Uses the real CatalogOfferPricer with a deterministic fake EUR/USD
    quote (no network), so the row carries exactly the provenance the
    doctor validates — never a hand-duplicated metadata dict.
    """
    import dataclasses
    from decimal import Decimal
    from uuid import uuid4 as _uuid4

    from cloud_platform.modules.offers.domain import PricingPolicy, SellableOffer
    from cloud_platform.modules.offers.pricing import CatalogOfferPricer

    base = SellableOffer(
        id=_uuid4(),
        provider_key="leaseweb",
        product_id="VPS02_1",
        location_id="FRA-01",
        name="Leaseweb VPS 1",
        vcpu=4,
        ram_gb=6,
        disk_gb=100,
        traffic="5 TB",
        provider_cost_minor=cost_minor,
        provider_cost_currency="EUR",
        selling_price_minor=0,
        selling_currency="EUR",
        billing_parameters={"provider_monthly_rate": str(Decimal(cost_minor) / 100)},
        billing_model="prepaid_monthly_fixed",
        provider_available=True,
        enabled=True,
        auto_priced=True,
    )
    priced = await CatalogOfferPricer(_DeterministicEurUsdRates(), "USD").price_auto(
        base, PricingPolicy(mode="markup", markup_percent=25, auto_publish=True)
    )
    return dataclasses.replace(
        base,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )


class TestCatalogAutoSyncDoctor:
    async def test_doctor_reports_configuration_and_status(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import cloud_platform.cli as cli_module

        settings.leaseweb_api_key = "TEST-CREDENTIAL"  # pragma: allowlist secret
        settings.hetzner_api_token = ""
        settings.storefront_pricing = {
            "leaseweb": {"mode": "markup", "markup_percent": 25, "auto_publish": True}
        }

        from datetime import UTC, datetime

        state_repo = MagicMock()
        state_repo.list_all = AsyncMock(
            return_value=[
                CatalogSyncState(
                    provider_key="leaseweb",
                    last_attempted_at=datetime.now(UTC),
                    last_success_at=datetime.now(UTC),
                    discovered=36,
                    persisted=35,
                    prices_updated=35,
                    published=35,
                    retired=1,
                    warnings=("detail enrichment lost",),
                    errors=(),
                )
            ]
        )
        import dataclasses

        sellable = await _usd_priced_offer()
        unpriced = dataclasses.replace(await _usd_priced_offer(), selling_price_minor=0)
        offers_repo = MagicMock()
        offers_repo.list_all = AsyncMock(return_value=[sellable, unpriced])
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemyCatalogSyncStateRepository",
            lambda session_factory: state_repo,
        )
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            lambda session_factory: offers_repo,
        )
        assert await cli_module.catalog_auto_sync_doctor() == 0
        out = capsys.readouterr().out
        assert "catalog auto-sync: enabled" in out
        assert "interval: 900s" in out
        assert "leaseweb:" in out
        assert "markup 25%" in out
        assert "auto publish: yes" in out
        assert "sellable: 1" in out
        assert "stored=2" in out
        assert "detail enrichment lost" in out
        # Presence only — the secret itself must never be printed.
        assert "TEST-CREDENTIAL" not in out

    async def test_doctor_renders_hourly_state_key_only_once(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        # The billing-suffixed "leaseweb.hourly" sync-state/policy key must
        # not surface as a second generic provider entry alongside the
        # dedicated monthly/hourly Leaseweb section.
        import cloud_platform.cli as cli_module

        settings.leaseweb_api_key = "TEST-CREDENTIAL"  # pragma: allowlist secret
        settings.hetzner_api_token = ""
        settings.storefront_pricing = {
            "leaseweb": {"mode": "markup", "markup_percent": 25, "auto_publish": True},
            "leaseweb.hourly": {
                "mode": "markup",
                "markup_percent": 25,
                "auto_publish": True,
            },
        }

        from datetime import UTC, datetime

        state_repo = MagicMock()
        state_repo.list_all = AsyncMock(
            return_value=[
                CatalogSyncState(provider_key="leaseweb"),
                CatalogSyncState(
                    provider_key="leaseweb.hourly",
                    last_attempted_at=datetime.now(UTC),
                    last_success_at=datetime.now(UTC),
                ),
            ]
        )
        offers_repo = MagicMock()
        offers_repo.list_all = AsyncMock(return_value=[])
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemyCatalogSyncStateRepository",
            lambda session_factory: state_repo,
        )
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            lambda session_factory: offers_repo,
        )
        assert await cli_module.catalog_auto_sync_doctor() == 0
        lines = capsys.readouterr().out.splitlines()
        assert sum(1 for line in lines if line == "leaseweb.hourly: (hourly)") == 1
        assert not any(line == "leaseweb.hourly:" for line in lines)
        assert "TEST-CREDENTIAL" not in "\n".join(lines)

    async def test_doctor_survives_router_failure(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import cloud_platform.cli as cli_module

        state_repo = MagicMock()
        state_repo.list_all = AsyncMock(return_value=[])
        offers_repo = MagicMock()
        offers_repo.list_all = AsyncMock(return_value=[])
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemyCatalogSyncStateRepository",
            lambda session_factory: state_repo,
        )
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            lambda session_factory: offers_repo,
        )
        monkeypatch.setattr(
            "cloud_platform.providers.leaseweb.accounts.build_leaseweb_account_router",
            MagicMock(side_effect=RuntimeError("router down")),
        )
        assert await cli_module.catalog_auto_sync_doctor() == 0
        assert "leaseweb:" in capsys.readouterr().out

    async def test_doctor_reports_unreadable_catalog(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import cloud_platform.cli as cli_module

        broken = MagicMock()
        broken.list_all = AsyncMock(side_effect=RuntimeError("db down"))
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemyCatalogSyncStateRepository",
            lambda session_factory: broken,
        )
        monkeypatch.setattr(
            "cloud_platform.modules.offers.repository.SqlAlchemySellableOfferRepository",
            lambda session_factory: broken,
        )
        assert await cli_module.catalog_auto_sync_doctor() == 1
        assert "unreadable" in capsys.readouterr().out
