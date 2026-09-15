"""FX currency math: identity, exact IRT/IRR, live routes, rounding.

All arithmetic uses Decimal; no test touches float. Rates are synthetic
contract-shaped fixtures (never live prices, never hardcoded production
assumptions beyond the route structure).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cloud_platform.modules.fx.cache import InMemoryFxCache
from cloud_platform.modules.fx.domain import (
    FxMarketQuote,
    FxPurpose,
    FxUnavailableError,
    currency_exponent,
    major_to_minor,
    minor_to_major,
)
from cloud_platform.modules.fx.formatting import format_minor, format_minor_signed
from cloud_platform.modules.fx.service import FxConfig, FxResolver


def _quote(
    base: str,
    buy: str,
    sell: str,
    *,
    market: str = "",
    observed: datetime | None = None,
    ttl: int = 60,
) -> FxMarketQuote:
    moment = observed or datetime.now(UTC)
    return FxMarketQuote(
        base_currency=base,
        quote_currency="IRT",
        buy_rate=Decimal(buy),
        sell_rate=Decimal(sell),
        source="abantether",
        source_market=market or (f"{base}IRT"),
        observed_at=moment,
        expires_at=moment + timedelta(seconds=ttl),
    )


class _Source:
    source_name = "abantether-test"

    def __init__(self, quotes: dict[str, FxMarketQuote]) -> None:
        self._quotes = quotes
        self.calls: list[str] = []
        self.closed = False

    async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
        self.calls.append(f"{base}->{quote}")
        key = f"{base}->{quote}"
        if key not in self._quotes:
            raise FxUnavailableError(f"no market {key}")
        return self._quotes[key]

    async def close(self) -> None:
        self.closed = True


def _resolver(**over: object) -> tuple[FxResolver, _Source, InMemoryFxCache]:
    quotes = {
        "EUR->IRT": _quote("EUR", "200000", "199000"),
        "USDT->IRT": _quote("USDT", "100000", "99000"),
    }
    source = _Source(quotes)
    cache = InMemoryFxCache()
    config = FxConfig(**over)  # type: ignore[arg-type]
    return FxResolver(source=source, cache=cache, config=config), source, cache


class TestExponents:
    def test_irt_irr_zero_decimal(self) -> None:
        assert currency_exponent("IRT") == 0
        assert currency_exponent("IRR") == 0

    def test_eur_usd_two_decimals(self) -> None:
        assert currency_exponent("EUR") == 2
        assert currency_exponent("USD") == 2

    def test_no_float_in_helpers(self) -> None:
        assert minor_to_major(1_250_000, "IRT") == Decimal("1250000")
        assert minor_to_major(499, "EUR") == Decimal("4.99")
        assert major_to_minor(Decimal("4.99"), "EUR", FxPurpose.DISPLAY) == 499


class TestIdentity:
    async def test_irt_identity_needs_no_provider_call(self) -> None:
        resolver, source, _ = _resolver()
        out = await resolver.resolve(1_250_000, "IRT", "IRT", FxPurpose.CHARGE)
        assert out.target_amount_minor == 1_250_000
        assert out.stale is False and out.proxy is False
        assert source.calls == []

    async def test_eur_identity(self) -> None:
        resolver, source, _ = _resolver()
        out = await resolver.resolve(499, "EUR", "EUR", FxPurpose.DISPLAY)
        assert out.target_amount_minor == 499
        assert source.calls == []

    async def test_usd_identity(self) -> None:
        resolver, source, _ = _resolver()
        out = await resolver.resolve(499, "USD", "USD", FxPurpose.DISPLAY)
        assert out.target_amount_minor == 499
        assert source.calls == []


class TestExactIrtIrr:
    async def test_irt_to_irr_x10(self) -> None:
        resolver, source, _ = _resolver()
        out = await resolver.resolve(50_000, "IRT", "IRR", FxPurpose.CHARGE)
        assert out.target_amount_minor == 500_000
        assert source.calls == []

    async def test_irr_to_irt_exact_tenth(self) -> None:
        resolver, source, _ = _resolver()
        out = await resolver.resolve(500_000, "IRR", "IRT", FxPurpose.DISPLAY)
        assert out.target_amount_minor == 50_000
        assert source.calls == []


class TestLiveRoutes:
    async def test_eur_to_irt_charge_uses_buy(self) -> None:
        resolver, _, _ = _resolver()
        out = await resolver.resolve(500, "EUR", "IRT", FxPurpose.CHARGE)  # 5 EUR
        assert out.target_amount_minor == 1_000_000  # 5 * 200000
        assert "EURIRT" in out.path and "buy" in out.path
        assert out.proxy is False

    async def test_irt_to_eur_display(self) -> None:
        resolver, _, _ = _resolver()
        out = await resolver.resolve(1_000_000, "IRT", "EUR", FxPurpose.DISPLAY)
        assert out.target_amount_minor == 500  # 1M / 200000
        assert out.proxy is False

    async def test_liquidation_uses_sell(self) -> None:
        resolver, _, _ = _resolver()
        out = await resolver.resolve(500, "EUR", "IRT", FxPurpose.LIQUIDATION)
        assert out.target_amount_minor == 995_000  # 5 * 199000
        assert "sell" in out.path

    async def test_eur_to_usd_through_irt(self) -> None:
        resolver, _, _ = _resolver()
        out = await resolver.resolve(500, "EUR", "USD", FxPurpose.DISPLAY)
        # 5 EUR * 200000 / 100000 = 10 USD
        assert out.target_amount_minor == 1_000
        assert out.proxy is True and out.proxy_asset == "USDT"

    async def test_usd_to_eur_through_irt(self) -> None:
        resolver, _, _ = _resolver()
        out = await resolver.resolve(1_000, "USD", "EUR", FxPurpose.DISPLAY)
        # 10 USD * 100000 / 200000 = 5 EUR
        assert out.target_amount_minor == 500
        assert out.proxy is True

    async def test_eur_to_irr_via_anchor(self) -> None:
        resolver, _, _ = _resolver()
        out = await resolver.resolve(500, "EUR", "IRR", FxPurpose.CHARGE)
        assert out.target_amount_minor == 10_000_000  # 1M IRT * 10


class TestRounding:
    async def test_charge_rounds_up_to_smallest_unit(self) -> None:
        quotes = {"EUR->IRT": _quote("EUR", "200000.5", "199000")}
        source = _Source(quotes)
        resolver = FxResolver(source=source, cache=InMemoryFxCache(), config=FxConfig())
        out = await resolver.resolve(1, "EUR", "IRT", FxPurpose.CHARGE)  # 0.01 EUR
        # 0.01 * 200000.5 = 2000.005 -> ceiling to 2001 (never undercharge)
        assert out.target_amount_minor == 2001

    async def test_display_rounds_half_up(self) -> None:
        assert major_to_minor(Decimal("2.005"), "EUR", FxPurpose.DISPLAY) == 201
        assert major_to_minor(Decimal("2.004"), "EUR", FxPurpose.DISPLAY) == 200


class TestFormatting:
    def test_irt_zero_decimal_grouped(self) -> None:
        assert format_minor(1_250_000, "IRT") == "1,250,000 تومان"

    def test_irt_never_divided_by_100(self) -> None:
        assert format_minor(50_000, "IRT") == "50,000 تومان"

    def test_eur_two_decimals(self) -> None:
        assert format_minor(499, "EUR") == "€4.99"

    def test_usd_two_decimals(self) -> None:
        assert format_minor(499, "USD") == "$4.99"

    def test_signed_variants(self) -> None:
        assert format_minor_signed(1_250_000, "IRT") == "+1,250,000 تومان"
        assert format_minor_signed(-150, "EUR") == "-€1.50"

    def test_no_float_anywhere(self) -> None:
        import inspect

        import cloud_platform.modules.fx.formatting as fmt
        import cloud_platform.modules.fx.service as svc

        for module in (fmt, svc):
            source = inspect.getsource(module)
            assert "float(" not in source
