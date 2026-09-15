"""FX catalog integration: native price untouched, display converted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from cloud_platform.bot.monthly_ui import MonthlyBotUi
from cloud_platform.modules.fx.cache import InMemoryFxCache
from cloud_platform.modules.fx.domain import FxMarketQuote, FxPurpose
from cloud_platform.modules.fx.service import FxConfig, FxResolver


def _quote(base: str, buy: str) -> FxMarketQuote:
    moment = datetime.now(UTC)
    return FxMarketQuote(
        base_currency=base,
        quote_currency="IRT",
        buy_rate=Decimal(buy),
        sell_rate=Decimal(buy),
        source="abantether",
        source_market=f"{base}IRT",
        observed_at=moment,
        expires_at=moment + timedelta(seconds=60),
    )


class _Source:
    source_name = "abantether-test"

    async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
        return _quote(base, "200000" if base == "EUR" else "100000")

    async def close(self) -> None:
        return None


class _Offers:
    def __init__(self, offers: list[Any]) -> None:
        self._offers = offers

    async def list_provider_locations(self) -> list[Any]:
        return []


class _View:
    def __init__(self, plans: list[Any]) -> None:
        self._plans = plans

    def provider_display_name(self, key: str) -> str:
        return key

    async def plans_screen(self, location_id: str, provider_key: str | None = None) -> Any:
        return self._plans, "back", "cancel"


class _Plan:
    def __init__(self, minor: int, currency: str) -> None:
        self.offer_id = uuid4()
        self.name = "VPS"
        self.vcpu = 2
        self.ram_gb = 4
        self.disk_gb = 80
        self.monthly_price_minor = minor
        self.currency = currency


def _ui(fx: Any) -> MonthlyBotUi:
    return MonthlyBotUi(
        "test-signing-key-for-fx-catalog-0001",
        offers_view=_View([]),  # type: ignore[arg-type]
        checkout=None,  # type: ignore[arg-type]
        servers=None,  # type: ignore[arg-type]
        orders=None,  # type: ignore[arg-type]
        renewals=None,  # type: ignore[arg-type]
        offers_repo=_Offers([]),  # type: ignore[arg-type]
        wallet_history=None,  # type: ignore[arg-type]
        fx_resolver=fx,
        fx_display_currency="IRT",
    )


class TestCatalogDisplay:
    async def test_eur_offer_displays_irt_equivalent(self) -> None:
        fx = FxResolver(source=_Source(), cache=InMemoryFxCache(), config=FxConfig())
        ui = _ui(fx)
        ui._view = _View([_Plan(499, "EUR")])  # type: ignore[assignment]
        screen = await ui.store_plans_screen("leaseweb", "AMS-01")
        buttons = [b.text for row in screen.keyboard.inline_keyboard for b in row]
        assert any("€4.99" in text for text in buttons)
        assert any("تومان" in text for text in buttons)  # supplementary converted figure

    async def test_same_currency_needs_no_fx_call(self) -> None:
        calls: list[str] = []

        class _Tracking(_Source):
            async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
                calls.append(f"{base}->{quote}")
                return await super().get_quote(base, quote)

        fx = FxResolver(source=_Tracking(), cache=InMemoryFxCache(), config=FxConfig())
        ui = _ui(fx)
        label = await ui._price_label(50_000, "IRT")
        assert label == "50,000 تومان"
        assert calls == []

    async def test_unavailable_fx_degrades_to_native_only(self) -> None:
        class _Down:
            source_name = "abantether-test"

            async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
                from cloud_platform.modules.fx.domain import FxUnavailableError

                raise FxUnavailableError("down")

            async def close(self) -> None:
                return None

        fx = FxResolver(source=_Down(), cache=InMemoryFxCache(), config=FxConfig())
        ui = _ui(fx)
        label = await ui._price_label(499, "EUR")
        assert label == "€4.99"  # native only: no fabricated, no zero price

    async def test_native_db_value_never_overwritten(self) -> None:
        plan = _Plan(499, "EUR")
        fx = FxResolver(source=_Source(), cache=InMemoryFxCache(), config=FxConfig())
        ui = _ui(fx)
        await ui._price_label(plan.monthly_price_minor, plan.currency)
        assert plan.monthly_price_minor == 499
        assert plan.currency == "EUR"

    async def test_eur_offer_displays_usd_through_proxy(self) -> None:
        fx = FxResolver(source=_Source(), cache=InMemoryFxCache(), config=FxConfig())
        resolved = await fx.resolve(500, "EUR", "USD", FxPurpose.DISPLAY)
        assert resolved.proxy is True and resolved.proxy_asset == "USDT"
