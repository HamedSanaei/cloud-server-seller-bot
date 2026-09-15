"""FX edge branches: guards, validation failures and degraded paths.

Every branch here is mainline-adjacent error handling of the new subsystem:
input guards, malformed provider/cache documents, degraded cache/client
behavior and the remaining routing legs. No live I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_platform.modules.fx.cache import (
    InMemoryFxCache,
    quote_from_document,
)
from cloud_platform.modules.fx.domain import (
    FxInvalidQuoteError,
    FxMarketQuote,
    FxPurpose,
    FxUnavailableError,
    FxUnsupportedCurrencyError,
    major_to_minor,
    minor_to_major,
    normalize_currency,
)
from cloud_platform.modules.fx.formatting import format_minor, format_minor_signed
from cloud_platform.modules.fx.service import FxConfig, FxResolver


def _quote(base: str = "EUR", buy: str = "200000") -> FxMarketQuote:
    moment = datetime.now(UTC)
    return FxMarketQuote(
        base_currency=base,
        quote_currency="IRT",
        buy_rate=Decimal(buy),
        sell_rate=Decimal(buy),
        source="abantether",
        source_market=f"{base}IRT",
        observed_at=moment,
        expires_at=moment + timedelta(seconds=3600),
    )


class _Source:
    source_name = "abantether-test"

    def __init__(self, quotes: dict[str, FxMarketQuote] | None = None) -> None:
        self._quotes = quotes or {"EUR->IRT": _quote("EUR"), "USDT->IRT": _quote("USDT")}

    async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
        return self._quotes[f"{base}->{quote}"]

    async def close(self) -> None:
        return None


def _resolver(**over: Any) -> FxResolver:
    return FxResolver(source=_Source(), cache=InMemoryFxCache(), config=FxConfig(**over))  # type: ignore[arg-type]


class TestDomainGuards:
    def test_normalize_rejects_garbage(self) -> None:
        for bad in ("", "EURO12", "E U", "TOOLONGCODE"):
            with pytest.raises(FxUnsupportedCurrencyError):
                normalize_currency(bad)

    def test_minor_to_major_rejects_bool(self) -> None:
        with pytest.raises(ValueError):
            minor_to_major(True, "EUR")  # type: ignore[arg-type]

    def test_major_to_minor_rejects_float(self) -> None:
        with pytest.raises(ValueError):
            major_to_minor(4.99, "EUR", FxPurpose.DISPLAY)  # type: ignore[arg-type]

    def test_quote_rejects_bad_rates_and_metadata(self) -> None:
        good = _quote()
        with pytest.raises(FxInvalidQuoteError):
            FxMarketQuote(
                base_currency="EUR",
                quote_currency="IRT",
                buy_rate=Decimal("0"),
                sell_rate=good.sell_rate,
                source="abantether",
                source_market="EURIRT",
                observed_at=good.observed_at,
                expires_at=good.expires_at,
            )
        with pytest.raises(FxInvalidQuoteError):
            FxMarketQuote(
                base_currency="EUR",
                quote_currency="IRT",
                buy_rate=good.buy_rate,
                sell_rate=Decimal("-1"),
                source="abantether",
                source_market="EURIRT",
                observed_at=good.observed_at,
                expires_at=good.expires_at,
            )
        with pytest.raises(FxInvalidQuoteError):
            FxMarketQuote(
                base_currency="EUR",
                quote_currency="IRT",
                buy_rate=good.buy_rate,
                sell_rate=good.sell_rate,
                source="",
                source_market="EURIRT",
                observed_at=good.observed_at,
                expires_at=good.expires_at,
            )
        with pytest.raises(FxInvalidQuoteError):
            FxMarketQuote(
                base_currency="EUR",
                quote_currency="IRT",
                buy_rate=good.buy_rate,
                sell_rate=good.sell_rate,
                source="abantether",
                source_market="",
                observed_at=good.observed_at,
                expires_at=good.expires_at,
            )

    def test_resolved_rejects_negative_amounts(self) -> None:
        from cloud_platform.modules.fx.domain import ResolvedMoney

        moment = datetime.now(UTC)
        with pytest.raises(ValueError):
            ResolvedMoney(
                source_amount_minor=-1,
                source_currency="EUR",
                target_amount_minor=0,
                target_currency="IRT",
                rate=Decimal("1"),
                purpose=FxPurpose.DISPLAY,
                source="abantether",
                path="x",
                observed_at=moment,
                expires_at=moment,
            )
        with pytest.raises(ValueError):
            ResolvedMoney(
                source_amount_minor=0,
                source_currency="EUR",
                target_amount_minor=-1,
                target_currency="IRT",
                rate=Decimal("1"),
                purpose=FxPurpose.DISPLAY,
                source="abantether",
                path="x",
                observed_at=moment,
                expires_at=moment,
            )


class TestFormattingGuards:
    def test_bool_amount_rejected(self) -> None:
        with pytest.raises(ValueError):
            format_minor(True, "EUR")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            format_minor_signed(False, "IRT")  # type: ignore[arg-type]

    def test_irr_and_unknown_codes(self) -> None:
        assert format_minor(12_500_000, "IRR") == "12,500,000 ریال"
        assert format_minor(199, "GBP") == "1.99 GBP"
        assert format_minor(0, "") == "0.00"
        assert format_minor_signed(-250, "GBP") == "-2.50 GBP"
        assert format_minor_signed(250, "GBP") == "+2.50 GBP"


class TestCacheDocuments:
    def test_non_dict_json_rejected(self) -> None:
        assert quote_from_document("[1, 2]") is None

    def test_unrecognised_doc_rejected(self) -> None:
        assert quote_from_document('{"a": 1}') is None

    async def test_clear(self) -> None:
        cache = InMemoryFxCache()
        await cache.put("EUR->IRT", _quote())
        cache.clear()
        assert await cache.get("EUR->IRT") is None


class TestResolverGuards:
    async def test_resolve_rejects_bad_amount(self) -> None:
        resolver = _resolver()
        with pytest.raises(ValueError):
            await resolver.resolve(True, "EUR", "IRT", FxPurpose.CHARGE)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            await resolver.resolve(-1, "EUR", "IRT", FxPurpose.CHARGE)

    async def test_purpose_string_coerced(self) -> None:
        resolver = _resolver()
        out = await resolver.resolve(100, "EUR", "EUR", "display")  # type: ignore[arg-type]
        assert out.target_amount_minor == 100

    async def test_disabled_config_fails_closed(self) -> None:
        resolver = _resolver(enabled=False)
        with pytest.raises(FxUnavailableError):
            await resolver.resolve(100, "EUR", "IRT", FxPurpose.CHARGE)
        assert await resolver.can_convert("EUR", "IRT", FxPurpose.DISPLAY) is False
        # Identity needs no provider, so it stays available.
        assert await resolver.can_convert("EUR", "EUR", FxPurpose.DISPLAY) is True

    async def test_can_convert_branches(self) -> None:
        resolver = _resolver()
        assert await resolver.can_convert("EUR", "EUR", FxPurpose.DISPLAY) is True
        assert await resolver.can_convert("IRT", "IRR", FxPurpose.CHARGE) is True
        assert await resolver.can_convert("EUR", "GBP", FxPurpose.DISPLAY) is False
        assert await resolver.can_convert("NOPE!", "IRT", FxPurpose.DISPLAY) is False

    async def test_snapshot_freezes(self) -> None:
        resolver = _resolver()
        snap = await resolver.snapshot(500, "EUR", "IRT", FxPurpose.CHARGE)
        assert snap.source_amount_minor == 500
        assert snap.target_amount_minor == 1_000_000

    async def test_irr_to_usd_anchor_leg(self) -> None:
        resolver = _resolver()
        out = await resolver.resolve(1_000_000, "IRR", "USD", FxPurpose.DISPLAY)
        assert out.target_currency == "USD"
        assert out.proxy is True

    async def test_unsupported_market_pair(self) -> None:
        resolver = _resolver()
        with pytest.raises(FxUnsupportedCurrencyError):
            await resolver.resolve(100, "USDT", "IRT", FxPurpose.DISPLAY)

    async def test_validate_quote_rejects_wrong_pair(self) -> None:
        resolver = _resolver()
        with pytest.raises(FxInvalidQuoteError):
            resolver._validate_quote(_quote("EUR"), "USDT", "IRT")
        with pytest.raises(FxInvalidQuoteError):
            resolver._validate_quote("nope", "EUR", "IRT")  # type: ignore[arg-type]

    async def test_non_fx_error_wrapped_as_unavailable(self) -> None:
        class _Boom:
            source_name = "abantether-test"

            async def get_quote(self, base: str, quote: str) -> Any:
                raise RuntimeError("weird transport glitch")

            async def close(self) -> None:
                return None

        resolver = FxResolver(source=_Boom(), cache=InMemoryFxCache(), config=FxConfig())  # type: ignore[arg-type]
        with pytest.raises(FxUnavailableError):
            await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)

    async def test_broken_cache_degrades_to_live(self) -> None:
        class _BadCache:
            async def get(self, key: str) -> Any:
                raise ConnectionError("cache down")

            async def put(self, key: str, quote: Any) -> None:
                raise ConnectionError("cache down")

            async def close(self) -> None:
                return None

        resolver = FxResolver(source=_Source(), cache=_BadCache(), config=FxConfig())  # type: ignore[arg-type]
        out = await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)
        assert out.target_amount_minor == 1_000_000

    async def test_close_tolerates_transport_errors(self) -> None:
        class _Flaky:
            async def close(self) -> None:
                raise ConnectionError("gone")

        resolver = FxResolver(source=_Flaky(), cache=_Flaky(), config=FxConfig())  # type: ignore[arg-type]
        await resolver.close()  # never raises


class TestClientGuards:
    def test_unknown_market_coin_rejected(self) -> None:
        from cloud_platform.providers.abantether_fx.client import _market_coin

        with pytest.raises(FxInvalidQuoteError):
            _market_coin("XXXIRT")

    def test_constructor_guards(self) -> None:
        from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient

        with pytest.raises(ValueError):
            AbanTetherFxClient(base_url="")
        with pytest.raises(ValueError):
            AbanTetherFxClient(base_url="https://x.test", timeout_seconds=0)
        with pytest.raises(ValueError):
            AbanTetherFxClient(base_url="https://x.test", ttl_seconds=0)
        with pytest.raises(ValueError):
            AbanTetherFxClient(base_url="https://x.test", eur_symbol="")

    async def test_unknown_base_rejected(self) -> None:
        from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient

        client = AbanTetherFxClient(base_url="https://ticker.test")
        client._client = AsyncMock()  # type: ignore[method-assign]
        with pytest.raises(FxInvalidQuoteError):
            await client.get_quote("GBP", "IRT")
        assert client._client.get.await_count == 0
        await client.close()

    async def test_http_error_status_rejected(self) -> None:
        import httpx

        from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient

        client = AbanTetherFxClient(base_url="https://ticker.test")
        response = httpx.Response(
            400,
            content=b"{}",
            headers={"Content-Type": "application/json"},
            request=httpx.Request("GET", "https://ticker.test/"),
        )
        client._client = AsyncMock()  # type: ignore[method-assign]
        client._client.get = AsyncMock(return_value=response)
        with pytest.raises(FxUnavailableError):
            await client.get_quote("EUR", "IRT")
        await client.close()

    async def test_close_tolerates_errors(self) -> None:
        from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient

        client = AbanTetherFxClient(base_url="https://ticker.test")
        client._client = AsyncMock()  # type: ignore[method-assign]
        client._client.aclose = AsyncMock(side_effect=ConnectionError("gone"))
        await client.close()  # never raises
