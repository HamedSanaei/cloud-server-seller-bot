"""Catalog refresh configuration boundaries and operator-visible diagnostics."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import cloud_platform.worker.settings as ws
from cloud_platform.core.config import Settings
from cloud_platform.modules.offers.auto_sync import AutoSyncRunReport, ProviderAutoSyncReport
from cloud_platform.modules.offers.domain import CatalogSyncState


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        leaseweb_api_key="k",
        leaseweb_api_base_url="https://api.test",
        leaseweb_locations="AMS-01,FRA-01",
        leaseweb_os_allowlist="",
        leaseweb_order_os_only_free=True,
        hetzner_api_token="",
        hetzner_accounts=[],
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


def _coordinator(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    coordinator = AsyncMock()
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

    async def test_no_providers_configured_does_nothing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings.leaseweb_api_key = ""
        settings.leaseweb_accounts = []
        settings.hetzner_api_token = ""
        settings.hetzner_accounts = []
        coordinator = _coordinator(monkeypatch)
        await ws.catalog_auto_sync({})
        coordinator.run.assert_not_awaited()


class TestCatalogAutoSyncSchedule:
    def test_custom_hour_dividing_interval(self, settings: Settings) -> None:
        settings.storefront_catalog_sync_interval_seconds = 600
        assert ws.catalog_auto_sync_minutes() == set(range(0, 60, 10))

    def test_non_dividing_interval_falls_back(self, settings: Settings) -> None:
        settings.storefront_catalog_sync_interval_seconds = 700
        assert ws.catalog_auto_sync_minutes() == set(range(0, 60, 15))

    def test_timeout_above_the_interval_is_clamped(self, settings: Settings) -> None:
        settings.storefront_catalog_sync_interval_seconds = 600
        settings.storefront_catalog_sync_timeout_seconds = 900
        assert ws.catalog_auto_sync_timeout() == 600

    def test_invalid_timeout_falls_back_to_the_default(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "storefront_catalog_sync_timeout_seconds", "oops")
        assert ws.catalog_auto_sync_timeout() == ws.CATALOG_AUTO_SYNC_TIMEOUT_SECONDS


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


class TestCatalogAutoSyncRunCommand:
    """`catalog auto-sync run`: one COMPLETE refresh on demand.

    The release transition needs the provider facts the storefront prices from
    (a row whose provider observation is stale or missing cannot be
    canonicalized at all), and an operator needs the same repair path. Both use
    the worker's own coordinator pass with the DEDICATED catalog budget, never a
    second implementation and never the generic 120s job timeout whose
    cancellation left a production catalog unpriced forever.
    """

    async def test_disabled_catalog_is_not_reported_as_success(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import cloud_platform.cli as cli_module

        settings.storefront_catalog_sync_enabled = False
        pass_once = AsyncMock()
        monkeypatch.setattr(ws, "run_catalog_auto_sync_once", pass_once)
        assert await cli_module.catalog_auto_sync_run() == 1
        assert "disabled by configuration" in capsys.readouterr().out
        pass_once.assert_not_awaited()

    async def test_run_reports_provider_counters(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import cloud_platform.cli as cli_module

        report = AutoSyncRunReport(
            ran=True,
            providers=(
                ProviderAutoSyncReport(
                    provider_key="leaseweb",
                    ok=True,
                    discovered=507,
                    persisted=507,
                    prices_updated=456,
                    published=456,
                ),
            ),
        )
        monkeypatch.setattr(ws, "run_catalog_auto_sync_once", AsyncMock(return_value=report))
        assert await cli_module.catalog_auto_sync_run() == 0
        out = capsys.readouterr().out
        assert f"timeout {ws.catalog_auto_sync_timeout()}s" in out
        assert (
            "leaseweb: ok=True discovered=507 persisted=507 prices=456 "
            "published=456 retired=0 warnings=0 errors=0"
        ) in out
        assert "catalog refresh completed" in out

    async def test_provider_errors_are_not_reported_as_success(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import cloud_platform.cli as cli_module

        report = AutoSyncRunReport(
            ran=True,
            providers=(
                ProviderAutoSyncReport(
                    provider_key="leaseweb", ok=False, errors=("provider unavailable",)
                ),
            ),
        )
        monkeypatch.setattr(ws, "run_catalog_auto_sync_once", AsyncMock(return_value=report))
        assert await cli_module.catalog_auto_sync_run() == 1
        assert "completed with provider errors: leaseweb" in capsys.readouterr().out

    async def test_skipped_run_is_not_reported_as_success(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        import cloud_platform.cli as cli_module

        monkeypatch.setattr(ws, "run_catalog_auto_sync_once", AsyncMock(return_value=None))
        assert await cli_module.catalog_auto_sync_run() == 1
        assert "did not run" in capsys.readouterr().out

    async def test_run_is_cancelled_at_its_own_budget(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        """The dedicated budget bounds the run (never an unbounded hang)."""
        import asyncio

        import cloud_platform.cli as cli_module

        async def _hang() -> Any:
            await asyncio.sleep(30)

        monkeypatch.setattr(ws, "catalog_auto_sync_timeout", lambda: 0.05)
        monkeypatch.setattr(ws, "run_catalog_auto_sync_once", _hang)
        assert await cli_module.catalog_auto_sync_run() == 1
        assert "was cancelled" in capsys.readouterr().out
