"""Read-only AbanTether FX client (public ticker, no API key).

Endpoint::

    GET {base_url}/api/v1/manager/otc/ticker?coin=EUR
    GET {base_url}/api/v1/manager/otc/ticker?coin=USDT

The response carries ``data.markets`` keyed by EXACT market names
(``EURIRT``, ``USDTIRT``). Only those two keys are ever read — substring
matching is forbidden (``AST_USDTBOXIRT`` must never match, ``EURI`` is a
different asset and is never used as EUR). ``active`` must be true and both
prices must be positive Decimals parsed from strings (never float).

This integration is READ ONLY: no trade/order endpoint is ever called.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from cloud_platform.modules.fx.domain import (
    FxInvalidQuoteError,
    FxMarketQuote,
    FxUnavailableError,
    parse_rate,
)
from cloud_platform.observability.metrics import metrics

logger = logging.getLogger(__name__)

SOURCE_NAME = "abantether"
DEFAULT_BASE_URL = "https://api.abantether.com"
TICKER_PATH = "/api/v1/manager/otc/ticker"

#: Exact market keys (never substring-matched).
EUR_MARKET = "EURIRT"
USDT_MARKET = "USDTIRT"


def _market_coin(market: str) -> str:
    if market == EUR_MARKET:
        return "EUR"
    if market == USDT_MARKET:
        return "USDT"
    raise FxInvalidQuoteError(f"unsupported AbanTether market {market!r}")


def parse_market_payload(
    payload: Any,
    *,
    market: str,
    expected_symbol: str,
    source: str = SOURCE_NAME,
    observed_at: datetime | None = None,
    ttl_seconds: int = 60,
) -> FxMarketQuote:
    """Validate one ticker payload and return the quote for ``market``.

    Raises :class:`FxInvalidQuoteError` for missing/inactive markets,
    malformed Decimals and non-positive prices. Pure function (unit-tested
    without HTTP).
    """
    moment = observed_at or datetime.now(UTC)
    if not isinstance(payload, dict):
        raise FxInvalidQuoteError("abantether ticker payload is not an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise FxInvalidQuoteError("abantether ticker payload has no data object")
    markets = data.get("markets")
    if not isinstance(markets, dict):
        raise FxInvalidQuoteError("abantether ticker payload has no markets object")
    entry = markets.get(market)
    if not isinstance(entry, dict):
        raise FxInvalidQuoteError(f"abantether market {market!r} is missing")
    symbol = str(entry.get("symbol", "")).strip().upper()
    if symbol != expected_symbol.upper():
        raise FxInvalidQuoteError(
            f"abantether market {market!r} carries symbol {symbol!r}, expected {expected_symbol!r}"
        )
    if entry.get("active") is not True:
        raise FxInvalidQuoteError(f"abantether market {market!r} is not active")
    buy_rate = parse_rate(entry.get("buy_price"), field="buy_price")
    sell_rate = parse_rate(entry.get("sell_price"), field="sell_price")
    base = expected_symbol.upper()
    return FxMarketQuote(
        base_currency=base,
        quote_currency="IRT",
        buy_rate=buy_rate,
        sell_rate=sell_rate,
        source=source,
        source_market=market,
        observed_at=moment,
        expires_at=moment + timedelta(seconds=ttl_seconds),
        proxy=False,
        proxy_asset="",
    )


class AbanTetherFxClient:
    """Async read-only AbanTether ticker client (implements ``FxRateSource``)."""

    source_name = SOURCE_NAME

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = 5.0,
        ttl_seconds: int = 60,
        eur_symbol: str = "EUR",
        usd_proxy_symbol: str = "USDT",
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("base_url must not be empty")
        self._base_url = base
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0")
        if not eur_symbol.strip() or not usd_proxy_symbol.strip():
            raise ValueError("eur_symbol and usd_proxy_symbol must not be empty")
        self._ttl_seconds = ttl_seconds
        self._eur_symbol = eur_symbol.strip().upper()
        self._usd_proxy_symbol = usd_proxy_symbol.strip().upper()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            logger.warning("abantether fx client close failed", exc_info=True)

    async def get_quote(self, base_currency: str, quote_currency: str) -> FxMarketQuote:
        """Fetch a fresh quote for ``EUR->IRT`` or ``USDT->IRT``."""
        base = (base_currency or "").strip().upper()
        quote = (quote_currency or "").strip().upper()
        if quote != "IRT":
            raise FxInvalidQuoteError(f"abantether only quotes against IRT (got {quote!r})")
        if base == self._eur_symbol:
            market, symbol = EUR_MARKET, self._eur_symbol
        elif base == self._usd_proxy_symbol:
            market, symbol = USDT_MARKET, self._usd_proxy_symbol
        else:
            raise FxInvalidQuoteError(
                f"abantether has no market for base {base!r} "
                f"(expected {self._eur_symbol!r} or {self._usd_proxy_symbol!r})"
            )
        payload = await self._fetch_market(market)
        try:
            return parse_market_payload(
                payload,
                market=market,
                expected_symbol=symbol,
                observed_at=datetime.now(UTC),
                ttl_seconds=self._ttl_seconds,
            )
        except FxInvalidQuoteError:
            raise
        except Exception as exc:  # pragma: no cover - defensive shape guard
            raise FxInvalidQuoteError(f"abantether market {market!r} is unusable") from exc

    async def _fetch_market(self, market: str) -> Any:
        coin = _market_coin(market)
        url = f"{self._base_url}{TICKER_PATH}"
        async with metrics.provider_call(self.source_name, "GET /ticker"):
            try:
                response = await self._client.get(url, params={"coin": coin})
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise FxUnavailableError(f"abantether ticker unreachable ({coin})") from exc
        status = response.status_code
        if status == 429:
            from cloud_platform.providers.errors import ProviderRateLimited

            raise ProviderRateLimited("abantether rate limit exceeded")
        if status >= 500:
            raise FxUnavailableError(f"abantether ticker unavailable (HTTP {status})")
        if response.is_error:
            raise FxUnavailableError(f"abantether ticker failed (HTTP {status})")
        try:
            return response.json()
        except Exception as exc:
            raise FxInvalidQuoteError("abantether ticker returned non-JSON payload") from exc

    def __repr__(self) -> str:  # pragma: no cover - no secrets to leak by design
        return f"AbanTetherFxClient(base_url={self._base_url!r})"


__all__ = [
    "DEFAULT_BASE_URL",
    "EUR_MARKET",
    "SOURCE_NAME",
    "TICKER_PATH",
    "USDT_MARKET",
    "AbanTetherFxClient",
    "parse_market_payload",
]
