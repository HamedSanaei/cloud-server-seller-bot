"""Async Frankfurter adapter for official global reference rates."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from cloud_platform.modules.fx.domain import (
    FxInvalidQuoteError,
    FxReferenceQuote,
    FxUnavailableError,
)
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.errors import ProviderRateLimited

SOURCE_NAME = "frankfurter"
DEFAULT_BASE_URL = "https://api.frankfurter.dev"
RATE_PATH = "/v2/rate"


def _normalize_currency(value: object, *, field: str) -> str:
    """Normalize an ASCII, three-letter currency code without a currency registry."""
    if not isinstance(value, str):
        raise FxInvalidQuoteError(f"{field} must be a three-letter currency code")
    currency = value.upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise FxInvalidQuoteError(f"{field} must be a three-letter currency code")
    return currency


def _validate_base_url(value: object) -> str:
    """Return a safe absolute HTTP(S) origin/base without URL credentials or query data."""
    if not isinstance(value, str):
        raise ValueError("base_url must be a string")
    base_url = value.strip()
    if not base_url:
        raise ValueError("base_url must not be empty")
    if any(character.isspace() for character in base_url):
        raise ValueError("base_url must not contain whitespace")
    if "?" in base_url or "#" in base_url:
        raise ValueError("base_url must not contain a query or fragment")

    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url must be a valid absolute HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.scheme.lower() == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("production Frankfurter base_url must use https://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain userinfo")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("base_url must contain a valid port")

    try:
        normalized = httpx.URL(base_url)
    except httpx.InvalidURL as exc:
        raise ValueError("base_url must be a valid absolute HTTP(S) URL") from exc
    return str(normalized).rstrip("/")


def _parse_reference_payload(
    payload: object,
    *,
    base: str,
    quote: str,
    observed_at: datetime,
    ttl_seconds: int,
) -> FxReferenceQuote:
    """Validate and convert Frankfurter's official flat JSON response."""
    if not isinstance(payload, dict):
        raise FxInvalidQuoteError("Frankfurter payload is not an object")
    if payload.get("base") != base:
        raise FxInvalidQuoteError("Frankfurter payload base does not match the request")
    if payload.get("quote") != quote:
        raise FxInvalidQuoteError("Frankfurter payload quote does not match the request")

    raw_rate = payload.get("rate")
    if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, Decimal)):
        raise FxInvalidQuoteError("Frankfurter payload rate is not numeric")
    rate = raw_rate if isinstance(raw_rate, Decimal) else Decimal(raw_rate)
    if not rate.is_finite() or rate <= 0:
        raise FxInvalidQuoteError("Frankfurter payload rate must be positive and finite")

    raw_date = payload.get("date")
    if not isinstance(raw_date, str):
        raise FxInvalidQuoteError("Frankfurter payload date is not a string")
    try:
        provider_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise FxInvalidQuoteError("Frankfurter payload date is invalid") from exc
    if raw_date != provider_date.isoformat():
        raise FxInvalidQuoteError("Frankfurter payload date is not canonical ISO format")

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise FxInvalidQuoteError("Frankfurter retrieval timestamp must be timezone-aware")

    return FxReferenceQuote(
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        source=SOURCE_NAME,
        source_market=f"{base}/{quote}",
        provider_date=provider_date,
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=ttl_seconds),
    )


class FrankfurterFxClient:
    """Read-only async Frankfurter reference-rate source."""

    source_name = SOURCE_NAME

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = 5.0,
        ttl_seconds: int = 3600,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite number")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")

        self._base_url = _validate_base_url(base_url)
        self._timeout_seconds = float(timeout_seconds)
        self._ttl_seconds = ttl_seconds
        self._closed = False
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            transport=transport,
        )

    async def close(self) -> None:
        """Close the owned HTTP client at most once."""
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def get_reference_rate(
        self,
        base_currency: str,
        quote_currency: str,
    ) -> FxReferenceQuote:
        """Fetch and validate the official reference rate for any 3-letter pair."""
        base = _normalize_currency(base_currency, field="base_currency")
        quote = _normalize_currency(quote_currency, field="quote_currency")
        endpoint = f"{self._base_url}{RATE_PATH}/{base.lower()}/{quote.lower()}"

        async with metrics.provider_call(self.source_name, "GET /v2/rate"):
            try:
                response = await self._client.get(endpoint)
            except httpx.RequestError as exc:
                raise FxUnavailableError(
                    f"Frankfurter reference rate {base}/{quote} is unavailable"
                ) from exc

            status = response.status_code
            if status == 429:
                raise ProviderRateLimited("Frankfurter reference rate limit exceeded")
            if not 200 <= status < 300:
                raise FxUnavailableError(
                    f"Frankfurter reference rate {base}/{quote} failed (HTTP {status})"
                )

            try:
                payload: object = response.json(parse_float=Decimal, parse_int=int)
            except (UnicodeError, ValueError):
                raise FxInvalidQuoteError("Frankfurter returned invalid JSON") from None

        return _parse_reference_payload(
            payload,
            base=base,
            quote=quote,
            observed_at=datetime.now(UTC),
            ttl_seconds=self._ttl_seconds,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(timeout_seconds={self._timeout_seconds!r}, "
            f"ttl_seconds={self._ttl_seconds!r}, closed={self._closed!r})"
        )


__all__ = [
    "DEFAULT_BASE_URL",
    "RATE_PATH",
    "SOURCE_NAME",
    "FrankfurterFxClient",
]
