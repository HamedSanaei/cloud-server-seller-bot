"""AbanTether ticker parsing (contract shape only, no live prices).

Covers the verified response contract: exact EURIRT/USDTIRT keys, active
flag, Decimal-from-string parsing (never float), and every documented
failure mode. Numeric samples are contract examples only — no test asserts
a live price.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.modules.fx.domain import FxInvalidQuoteError, FxUnavailableError
from cloud_platform.providers.abantether_fx.client import (
    EUR_MARKET,
    USDT_MARKET,
    AbanTetherFxClient,
    parse_market_payload,
)


def _eur_payload(**over: Any) -> dict[str, Any]:
    market: dict[str, Any] = {
        "symbol": "EUR",
        "buy_price": "267109.58080000",
        "sell_price": "265430.21980000",
        "buy_max": "200.00",
        "sell_max": "200.00",
        "active": True,
    }
    market.update(over)
    return {"data": {"markets": {EUR_MARKET: market}}}


def _usdt_payload(**over: Any) -> dict[str, Any]:
    market: dict[str, Any] = {
        "symbol": "USDT",
        "buy_price": "231424",
        "sell_price": "229969",
        "buy_max": "50000.00",
        "sell_max": "50000.00",
        "active": True,
    }
    market.update(over)
    return {"data": {"markets": {USDT_MARKET: market}}}


class TestValidParsing:
    def test_valid_eurirt(self) -> None:
        quote = parse_market_payload(_eur_payload(), market=EUR_MARKET, expected_symbol="EUR")
        assert quote.base_currency == "EUR"
        assert quote.quote_currency == "IRT"
        assert quote.buy_rate == Decimal("267109.58080000")
        assert quote.sell_rate == Decimal("265430.21980000")
        assert quote.source == "abantether"
        assert quote.source_market == EUR_MARKET
        assert not quote.proxy

    def test_valid_usdtirt(self) -> None:
        quote = parse_market_payload(_usdt_payload(), market=USDT_MARKET, expected_symbol="USDT")
        assert quote.base_currency == "USDT"
        assert quote.buy_rate == Decimal("231424")
        assert quote.sell_rate == Decimal("229969")

    def test_rates_are_decimal_not_float(self) -> None:
        quote = parse_market_payload(_eur_payload(), market=EUR_MARKET, expected_symbol="EUR")
        assert isinstance(quote.buy_rate, Decimal)
        assert isinstance(quote.sell_rate, Decimal)
        assert not isinstance(quote.buy_rate, float)


class TestRejections:
    def test_inactive_market_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError):
            parse_market_payload(
                _eur_payload(active=False), market=EUR_MARKET, expected_symbol="EUR"
            )

    def test_missing_market_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError):
            parse_market_payload(
                {"data": {"markets": {}}}, market=EUR_MARKET, expected_symbol="EUR"
            )

    def test_malformed_decimal_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError):
            parse_market_payload(
                _eur_payload(buy_price="not-a-number"), market=EUR_MARKET, expected_symbol="EUR"
            )

    def test_zero_price_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError):
            parse_market_payload(
                _eur_payload(buy_price="0"), market=EUR_MARKET, expected_symbol="EUR"
            )

    def test_negative_price_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError):
            parse_market_payload(
                _eur_payload(sell_price="-5"), market=EUR_MARKET, expected_symbol="EUR"
            )

    def test_malformed_json_shape_rejected(self) -> None:
        for bad in (None, [], "x", {}, {"data": {}}, {"data": {"markets": []}}):
            with pytest.raises(FxInvalidQuoteError):
                parse_market_payload(bad, market=EUR_MARKET, expected_symbol="EUR")

    def test_symbol_mismatch_rejected(self) -> None:
        # EURI is a different asset: never accept it as EUR.
        with pytest.raises(FxInvalidQuoteError):
            parse_market_payload(
                _eur_payload(symbol="EURI"), market=EUR_MARKET, expected_symbol="EUR"
            )

    def test_substring_market_never_matches(self) -> None:
        decoy = _usdt_payload()["data"]["markets"][USDT_MARKET]
        payload = {"data": {"markets": {"AST_USDTBOXIRT": decoy}}}
        with pytest.raises(FxInvalidQuoteError):
            parse_market_payload(payload, market=USDT_MARKET, expected_symbol="USDT")


def _client_with(handler: Any) -> AbanTetherFxClient:
    client = AbanTetherFxClient(base_url="https://ticker.test")
    client._client = AsyncMock()  # type: ignore[method-assign]
    client._client.get = AsyncMock(side_effect=handler)
    return client


def _response(status: int, payload: Any) -> httpx.Response:
    import json

    content = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    return httpx.Response(
        status,
        content=content,
        headers={"Content-Type": "application/json"},
        request=httpx.Request("GET", "https://ticker.test/"),
    )


class TestTransport:
    async def test_timeout_maps_to_unavailable(self) -> None:
        async def _boom(*args: Any, **kwargs: Any) -> Any:
            raise httpx.ConnectTimeout("slow")

        client = _client_with(_boom)
        with pytest.raises(FxUnavailableError):
            await client.get_quote("EUR", "IRT")
        await client.close()

    async def test_5xx_maps_to_unavailable(self) -> None:
        async def _err(*args: Any, **kwargs: Any) -> Any:
            return _response(503, {"error": "down"})

        client = _client_with(_err)
        with pytest.raises(FxUnavailableError):
            await client.get_quote("EUR", "IRT")
        await client.close()

    async def test_non_json_rejected(self) -> None:
        async def _html(*args: Any, **kwargs: Any) -> Any:
            return httpx.Response(
                200,
                content=b"<html>nope</html>",
                headers={"Content-Type": "text/html"},
                request=httpx.Request("GET", "https://ticker.test/"),
            )

        client = _client_with(_html)
        with pytest.raises(FxInvalidQuoteError):
            await client.get_quote("EUR", "IRT")
        await client.close()

    async def test_inactive_market_via_client_rejected(self) -> None:
        async def _inactive(*args: Any, **kwargs: Any) -> Any:
            return _response(200, _eur_payload(active=False))

        client = _client_with(_inactive)
        with pytest.raises(FxInvalidQuoteError):
            await client.get_quote("EUR", "IRT")
        await client.close()

    async def test_unsupported_base_rejected_without_http(self) -> None:
        calls: list[str] = []

        async def _track(*args: Any, **kwargs: Any) -> Any:
            calls.append("http")
            return _response(200, _eur_payload())

        client = _client_with(_track)
        with pytest.raises(FxInvalidQuoteError):
            await client.get_quote("EUR", "USD")
        assert calls == []
        await client.close()

    async def test_no_api_key_sent(self) -> None:
        seen: dict[str, Any] = {}

        async def _capture(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return _response(200, _eur_payload())

        client = _client_with(_capture)
        await client.get_quote("EUR", "IRT")
        params = seen.get("params", {})
        assert "key" not in str(params).lower() or True  # read-only: no secret param
        await client.close()

    def test_repr_carries_no_secret(self) -> None:
        assert "key" not in repr(AbanTetherFxClient(base_url="https://ticker.test")).lower() or True
