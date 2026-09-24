"""Focused contract tests for the Frankfurter infrastructure adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from cloud_platform.modules.fx.domain import FxInvalidQuoteError, FxUnavailableError
from cloud_platform.providers.errors import ProviderRateLimited
from cloud_platform.providers.frankfurter_fx import FrankfurterFxClient
from cloud_platform.providers.frankfurter_fx.client import _parse_reference_payload


def _json_response(
    request: httpx.Request,
    body: str,
    *,
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=body.encode(),
        headers={"content-type": "application/json"},
        request=request,
    )


def _quote_body(
    *,
    base: str = "EUR",
    quote: str = "USD",
    provider_date: str = "2025-07-18",
    rate: str = "1.145",
) -> str:
    return f'{{"date":"{provider_date}","base":"{base}","quote":"{quote}","rate":{rate}}}'


@pytest.mark.parametrize(
    ("base", "quote", "rate_text"),
    [
        ("EUR", "USD", "1.145"),
        ("GBP", "USD", "1.3428"),
        ("JPY", "USD", "0.00671"),
        ("SGD", "USD", "0.7412"),
        ("AUD", "USD", "0.6543"),
        ("CAD", "USD", "0.7281"),
    ],
)
async def test_official_shaped_quotes_use_requested_pair_and_reference_contract(
    base: str,
    quote: str,
    rate_text: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _json_response(request, _quote_body(base=base, quote=quote, rate=rate_text))

    client = FrankfurterFxClient(
        "https://frankfurter.test",
        ttl_seconds=900,
        transport=httpx.MockTransport(handler),
    )
    try:
        before = datetime.now(UTC)
        result = await client.get_reference_rate(base, quote)
        after = datetime.now(UTC)
    finally:
        await client.close()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == f"/v2/rate/{base.lower()}/{quote.lower()}"
    assert request.url.query == b""
    assert request.url.userinfo == b""
    assert "authorization" not in {name.lower() for name in request.headers}
    assert "x-api-key" not in {name.lower() for name in request.headers}

    assert result.base_currency == base
    assert result.quote_currency == quote
    assert result.rate == Decimal(rate_text)
    assert isinstance(result.rate, Decimal)
    assert result.source == "frankfurter"
    assert result.source_market == f"{base}/{quote}"
    assert result.provider_date == date.fromisoformat("2025-07-18")
    assert result.observed_at.tzinfo is not None
    assert result.observed_at.utcoffset() is not None
    assert result.expires_at == result.observed_at + timedelta(seconds=900)
    assert before <= result.observed_at <= after


async def test_numeric_rate_is_parsed_as_decimal_without_precision_loss() -> None:
    rate_text = "1.123456789012345678901234567890"

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, _quote_body(rate=rate_text))

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()

    assert result.rate == Decimal(rate_text)
    assert str(result.rate) == rate_text


async def test_base_url_path_is_preserved_before_official_endpoint() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _json_response(request, _quote_body())

    client = FrankfurterFxClient(
        "https://frankfurter.test/provider-root/",
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.get_reference_rate("eur", "usd")
    finally:
        await client.close()

    assert paths == ["/provider-root/v2/rate/eur/usd"]


@pytest.mark.parametrize(
    "currency",
    ["", "EU", "EURO", "E1R", "EUR ", "ËUR"],
)
async def test_invalid_base_currency_never_calls_provider(currency: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(request, _quote_body())

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxInvalidQuoteError):
            await client.get_reference_rate(currency, "USD")
    finally:
        await client.close()

    assert calls == 0


@pytest.mark.parametrize(
    "currency",
    ["", "US", "USDD", "U$D", " USD"],
)
async def test_invalid_quote_currency_never_calls_provider(currency: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _json_response(request, _quote_body())

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxInvalidQuoteError):
            await client.get_reference_rate("EUR", currency)
    finally:
        await client.close()

    assert calls == 0


async def test_arbitrary_three_letter_pair_is_not_blocked_by_adapter() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _json_response(request, _quote_body(base="AAA", quote="BBB"))

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.get_reference_rate("AAA", "BBB")
    finally:
        await client.close()

    assert paths == ["/v2/rate/aaa/bbb"]
    assert result.base_currency == "AAA"
    assert result.quote_currency == "BBB"


@pytest.mark.parametrize(
    "body",
    [
        "[]",
        "{}",
        '{"date":"2025-07-18","base":"GBP","quote":"USD","rate":1.145}',
        '{"date":"2025-07-18","base":"EUR","quote":"JPY","rate":1.145}',
        '{"base":"EUR","quote":"USD","rate":1.145}',
        '{"date":"2025-07-18","quote":"USD","rate":1.145}',
        '{"date":"2025-07-18","base":"EUR","quote":"USD"}',
    ],
)
async def test_malformed_payloads_are_rejected(body: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, body)

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxInvalidQuoteError):
            await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()


@pytest.mark.parametrize(
    "provider_date",
    ["", "2025-02-30", "2025-7-18", "20250718", "2025-07-18T00:00:00Z"],
)
async def test_invalid_provider_dates_are_rejected(provider_date: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, _quote_body(provider_date=provider_date))

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxInvalidQuoteError):
            await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()


@pytest.mark.parametrize(
    "rate",
    ["0", "-0.01", "NaN", "Infinity", "-Infinity", "true", '"1.145"', "null"],
)
async def test_non_positive_non_finite_or_non_numeric_rates_are_rejected(rate: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, _quote_body(rate=rate))

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxInvalidQuoteError):
            await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()


async def test_non_json_payload_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, "not-json")

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxInvalidQuoteError):
            await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("offline"),
    ],
)
async def test_timeout_and_network_failures_map_to_fx_unavailable(
    failure: httpx.RequestError,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxUnavailableError):
            await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()


@pytest.mark.parametrize("status_code", [400, 404, 500, 502, 503])
async def test_http_failures_map_to_fx_unavailable(status_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, _quote_body(), status_code=status_code)

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FxUnavailableError):
            await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()


async def test_rate_limit_uses_existing_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, _quote_body(), status_code=429)

    client = FrankfurterFxClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderRateLimited):
            await client.get_reference_rate("EUR", "USD")
    finally:
        await client.close()


class _CountingTransport(httpx.MockTransport):
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        super().__init__(handler)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


async def test_close_is_idempotent_and_closes_owned_transport_once() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(request, _quote_body())

    transport = _CountingTransport(handler)
    client = FrankfurterFxClient(transport=transport)
    await client.close()
    await client.close()

    assert transport.close_calls == 1
    assert client._client.is_closed


async def test_repr_omits_base_url_and_configured_timeout_is_applied() -> None:
    client = FrankfurterFxClient("https://private.frankfurter.test", timeout_seconds=2.5)
    try:
        rendered = repr(client)
        assert "private.frankfurter.test" not in rendered
        assert "https://" not in rendered
        assert "timeout_seconds=2.5" in rendered
        assert client._client.timeout.connect == 2.5
    finally:
        await client.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "frankfurter.test",
        "ftp://frankfurter.test",
        "https://",
        "https://user:password@frankfurter.test",  # pragma: allowlist secret
        "https://frankfurter.test?token=secret",
        "https://frankfurter.test#secret",
    ],
)
async def test_unsafe_or_non_absolute_base_urls_are_rejected(base_url: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        FrankfurterFxClient(base_url)

    rendered = str(exc_info.value)
    assert "secret" not in rendered
    assert "password" not in rendered


async def test_http_base_url_is_allowed_for_non_production_configuration() -> None:
    client = FrankfurterFxClient("http://localhost:8080")
    await client.close()


class TestClientConfigurationContract:
    """Configuration validation must fail closed before any network use."""

    @pytest.mark.parametrize("base_url", [None, 123, b"https://frankfurter.test"])
    def test_non_string_base_url_is_rejected(self, base_url: object) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            FrankfurterFxClient(base_url)  # type: ignore[arg-type]

    @pytest.mark.parametrize("base_url", ["", "   "])
    def test_blank_base_url_is_rejected(self, base_url: str) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            FrankfurterFxClient(base_url)

    def test_inner_whitespace_base_url_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not contain whitespace"):
            FrankfurterFxClient("https://api frankfurter.test")

    def test_unparsable_authority_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="valid absolute HTTP"):
            FrankfurterFxClient("https://frankfurter.test:99999")

    @pytest.mark.parametrize("base_url", ["http://frankfurter.test", "http://93.184.216.34"])
    def test_plaintext_remote_base_url_is_rejected(self, base_url: str) -> None:
        with pytest.raises(ValueError, match="must use https"):
            FrankfurterFxClient(base_url)

    def test_port_zero_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="valid port"):
            FrankfurterFxClient("https://frankfurter.test:0")

    @pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan")])
    def test_non_positive_or_non_finite_timeout_is_rejected(self, timeout: float) -> None:
        with pytest.raises(ValueError, match="timeout_seconds"):
            FrankfurterFxClient("https://frankfurter.test", timeout_seconds=timeout)

    @pytest.mark.parametrize("ttl", [True, 0, -5])
    def test_non_positive_ttl_is_rejected(self, ttl: int) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            FrankfurterFxClient("https://frankfurter.test", ttl_seconds=ttl)

    @pytest.mark.parametrize(
        "bad_currency", [None, 12, object(), "EURO", "EU1", "\u20ac\u20ac\u20ac"]
    )
    async def test_malformed_currency_is_rejected_before_any_request(
        self, bad_currency: object
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return _json_response(request, _quote_body())

        client = FrankfurterFxClient(
            "https://frankfurter.test", transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises(FxInvalidQuoteError):
                await client.get_reference_rate(bad_currency, "USD")  # type: ignore[arg-type]
            with pytest.raises(FxInvalidQuoteError):
                await client.get_reference_rate("EUR", bad_currency)  # type: ignore[arg-type]
            assert requests == []
        finally:
            await client.close()


def test_reference_payload_requires_timezone_aware_retrieval_timestamp() -> None:
    """A naive retrieval clock cannot express quote freshness, so it is refused."""
    with pytest.raises(FxInvalidQuoteError, match="timezone-aware"):
        _parse_reference_payload(
            {"base": "EUR", "quote": "USD", "rate": Decimal("1.1"), "date": "2025-07-18"},
            base="EUR",
            quote="USD",
            observed_at=datetime(2025, 7, 18, 12, 0),
            ttl_seconds=60,
        )
