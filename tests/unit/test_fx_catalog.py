"""FX catalog integration: native price untouched, display converted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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


class TestCatalogDisplay:
    async def test_eur_offer_displays_usd_through_proxy(self) -> None:
        fx = FxResolver(source=_Source(), cache=InMemoryFxCache(), config=FxConfig())
        resolved = await fx.resolve(500, "EUR", "USD", FxPurpose.DISPLAY)
        assert resolved.proxy is True and resolved.proxy_asset == "USDT"
