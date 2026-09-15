"""USD/USDT proxy rules: explicit for display, rejected for settlement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cloud_platform.modules.fx.cache import InMemoryFxCache
from cloud_platform.modules.fx.domain import (
    FxMarketQuote,
    FxProxyNotAllowedError,
    FxPurpose,
)
from cloud_platform.modules.fx.service import FxConfig, FxResolver


def _quote(base: str, buy: str, sell: str) -> FxMarketQuote:
    moment = datetime.now(UTC)
    return FxMarketQuote(
        base_currency=base,
        quote_currency="IRT",
        buy_rate=Decimal(buy),
        sell_rate=Decimal(sell),
        source="abantether",
        source_market=f"{base}IRT",
        observed_at=moment,
        expires_at=moment + timedelta(seconds=60),
    )


class _Source:
    source_name = "abantether-test"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
        self.calls.append(f"{base}->{quote}")
        if base == "EUR":
            return _quote("EUR", "200000", "199000")
        return _quote("USDT", "100000", "99000")

    async def close(self) -> None:
        return None


def _resolver(**over: object) -> FxResolver:
    return FxResolver(source=_Source(), cache=InMemoryFxCache(), config=FxConfig(**over))  # type: ignore[arg-type]


class TestDisplayProxy:
    async def test_display_allowed_with_proxy_metadata(self) -> None:
        resolver = _resolver()
        out = await resolver.resolve(1_000_000, "IRT", "USD", FxPurpose.DISPLAY)
        assert out.proxy is True
        assert out.proxy_asset == "USDT"
        assert out.target_currency == "USD"

    async def test_eur_usd_display_carries_proxy(self) -> None:
        resolver = _resolver()
        out = await resolver.resolve(500, "EUR", "USD", FxPurpose.DISPLAY)
        assert out.proxy is True and out.proxy_asset == "USDT"


class TestSettlementProxy:
    async def test_settlement_rejected_by_default(self) -> None:
        resolver = _resolver()  # allow_usdt_proxy_for_settlement=False
        with pytest.raises(FxProxyNotAllowedError):
            await resolver.resolve(500, "EUR", "USD", FxPurpose.CHARGE)
        with pytest.raises(FxProxyNotAllowedError):
            await resolver.resolve(1_000, "USD", "IRT", FxPurpose.CHARGE)

    async def test_settlement_allowed_only_with_explicit_flag(self) -> None:
        resolver = _resolver(allow_usdt_proxy_for_settlement=True)
        out = await resolver.resolve(1_000, "USD", "IRT", FxPurpose.CHARGE)
        assert out.proxy is True and out.proxy_asset == "USDT"

    async def test_display_flag_off_rejects_display(self) -> None:
        resolver = _resolver(allow_usdt_proxy_for_display=False)
        with pytest.raises(FxProxyNotAllowedError):
            await resolver.resolve(1_000_000, "IRT", "USD", FxPurpose.DISPLAY)

    async def test_no_silent_usd_equals_usdt(self) -> None:
        # Every USD result must say proxy=true; silence is a bug.
        resolver = _resolver()
        out = await resolver.resolve(1_000_000, "IRT", "USD", FxPurpose.DISPLAY)
        assert out.proxy is True
        assert out.proxy_asset == "USDT"
        assert "proxy" in out.path.lower() or out.proxy
