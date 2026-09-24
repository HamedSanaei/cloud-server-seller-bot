"""Fail-closed contracts of the global (non-domestic) Frankfurter FX resolver.

These tests pin the behavior an operator depends on during a provider outage:
the resolver may serve a *bounded* stale official reference rate to the
catalog, must never do so for a payment-bound caller, must never hide a
malformed provider payload behind a stale rate, and must never guess a rate.
No network, no clock manipulation: quotes and their observation timestamps
are injected explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cloud_platform.modules.fx.cache import build_fx_cache
from cloud_platform.modules.fx.domain import (
    FxInvalidQuoteError,
    FxPurpose,
    FxReferenceQuote,
    FxStaleError,
    FxUnavailableError,
    FxUnsupportedCurrencyError,
)
from cloud_platform.modules.fx.ports import reference_rate_cache_key
from cloud_platform.modules.fx.service import (
    GlobalFiatFxConfig,
    GlobalFiatFxResolver,
    ReferenceRateResolution,
)


def _quote(
    base: str,
    quote: str,
    rate: str,
    *,
    source: str = "frankfurter",
    source_market: str | None = None,
    age_seconds: int = 0,
    ttl_seconds: int = 3600,
) -> FxReferenceQuote:
    observed_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return FxReferenceQuote(
        base_currency=base,
        quote_currency=quote,
        rate=Decimal(rate),
        source=source,
        source_market=source_market or f"{base}/{quote}",
        provider_date=observed_at.date(),
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=ttl_seconds),
    )


@dataclass
class _Source:
    """Deterministic reference source that records every call."""

    source_name: str = "frankfurter"
    rate: str = "1.10"
    error: Exception | None = None
    quote_factory: Callable[[str, str], FxReferenceQuote] | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    closed: int = 0

    async def get_reference_rate(self, base_currency: str, quote_currency: str) -> FxReferenceQuote:
        self.calls.append((base_currency, quote_currency))
        if self.error is not None:
            raise self.error
        if self.quote_factory is not None:
            return self.quote_factory(base_currency, quote_currency)
        return _quote(base_currency, quote_currency, self.rate)

    async def close(self) -> None:
        self.closed += 1


class _StubCache:
    """Minimal cache port: returns one fixed value and records puts."""

    def __init__(self, value: object = None, *, raise_on_get: bool = False) -> None:
        self.value = value
        self._raise_on_get = raise_on_get
        self.puts: list[object] = []

    async def get(self, key: str) -> object:
        if self._raise_on_get:
            raise RuntimeError("cache backing store unavailable")
        return self.value

    async def put(self, key: str, quote: object) -> None:
        self.puts.append(quote)


def _resolver(
    source: _Source,
    *,
    cache: object | None = None,
    quote_ttl_seconds: int = 3600,
    max_stale_seconds: int = 345600,
) -> GlobalFiatFxResolver:
    return GlobalFiatFxResolver(
        source=source,  # type: ignore[arg-type]
        cache=cache if cache is not None else build_fx_cache(backend="memory"),  # type: ignore[arg-type]
        config=GlobalFiatFxConfig(
            quote_ttl_seconds=quote_ttl_seconds,
            max_stale_seconds=max_stale_seconds,
        ),
    )


class TestGlobalFiatFxConfigContract:
    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"enabled": "yes"}, "enabled must be boolean"),
            ({"catalog_currency": "IRT"}, "audited global fiat"),
            ({"quote_ttl_seconds": True}, "quote_ttl_seconds must be an integer"),
            ({"max_stale_seconds": True}, "max_stale_seconds must be an integer"),
            ({"quote_ttl_seconds": 0}, "quote_ttl_seconds must be > 0"),
            (
                {"quote_ttl_seconds": 600, "max_stale_seconds": 60},
                "max_stale_seconds must be >=",
            ),
            ({"catalog_max_stale_seconds": True}, "catalog_max_stale_seconds must be an integer"),
            (
                {
                    "quote_ttl_seconds": 60,
                    "max_stale_seconds": 100,
                    "catalog_max_stale_seconds": 10,
                },
                "catalog_max_stale_seconds must be >=",
            ),
            (
                {
                    "quote_ttl_seconds": 60,
                    "max_stale_seconds": 100,
                    "catalog_max_stale_seconds": 200,
                },
                "catalog_max_stale_seconds must be <=",
            ),
        ],
    )
    def test_invalid_policy_is_rejected(self, kwargs: dict[str, object], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            GlobalFiatFxConfig(**kwargs)  # type: ignore[arg-type]

    def test_catalog_stale_window_defaults_to_the_global_stale_window(self) -> None:
        default = GlobalFiatFxConfig()
        assert default.catalog_max_stale_seconds == default.max_stale_seconds == 345600
        narrowed = GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600)
        assert narrowed.catalog_max_stale_seconds == 3600


class TestReferenceRateResolutionContract:
    def test_non_quote_is_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="must be FxReferenceQuote"):
            ReferenceRateResolution(quote="EUR/USD")  # type: ignore[arg-type]

    def test_non_boolean_stale_flag_is_rejected(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="stale flag must be boolean"):
            ReferenceRateResolution(quote=_quote("EUR", "USD", "1.1"), stale="yes")  # type: ignore[arg-type]

    def test_provenance_properties_project_the_quote(self) -> None:
        quote = _quote("EUR", "USD", "1.145")
        resolution = ReferenceRateResolution(quote, stale=True)

        assert resolution.base_currency == "EUR"
        assert resolution.quote_currency == "USD"
        assert resolution.rate == Decimal("1.145")
        assert resolution.source == "frankfurter"
        assert resolution.observed_at == quote.observed_at
        # The provider business date is retained as midnight UTC for audit.
        assert resolution.provider_date == datetime.combine(
            quote.provider_date, datetime.min.time(), tzinfo=UTC
        )
        assert resolution.provider_date.tzinfo is not None


class TestResolverInputContract:
    async def test_allow_catalog_stale_must_be_boolean(self) -> None:
        resolver = _resolver(_Source())
        with pytest.raises(ValueError, match="allow_catalog_stale must be boolean"):
            await resolver.get_rate("EUR", "USD", allow_catalog_stale="yes")  # type: ignore[arg-type]

    @pytest.mark.parametrize(("base", "quote"), [("IRT", "USD"), ("USD", "IRR"), ("XAU", "USD")])
    async def test_non_global_fiat_pair_is_refused(self, base: str, quote: str) -> None:
        source = _Source()
        resolver = _resolver(source)

        with pytest.raises(FxUnsupportedCurrencyError):
            await resolver.get_rate(base, quote)
        assert source.calls == []

    async def test_disabled_global_fx_fails_closed(self) -> None:
        source = _Source()
        resolver = GlobalFiatFxResolver(
            source=source,  # type: ignore[arg-type]
            cache=build_fx_cache(backend="memory"),
            config=GlobalFiatFxConfig(enabled=False),
        )

        with pytest.raises(FxUnavailableError, match="disabled"):
            await resolver.get_rate("EUR", "USD")
        assert source.calls == []

    async def test_non_frankfurter_source_family_is_refused(self) -> None:
        source = _Source(source_name="abantether")
        resolver = _resolver(source)

        with pytest.raises(FxInvalidQuoteError, match="must be Frankfurter"):
            await resolver.get_rate("EUR", "USD")
        assert source.calls == []

    async def test_catalog_currency_is_reported(self) -> None:
        assert _resolver(_Source()).catalog_currency == "USD"


class TestResolverStalePolicy:
    """Bounded stale catalog pricing vs. fail-closed payment conversion."""

    async def test_seeded_expired_quote_is_used_for_catalog_only(self) -> None:
        quote = _quote("EUR", "USD", "1.10", age_seconds=120, ttl_seconds=60)
        cache = build_fx_cache(backend="memory")
        await cache.put(reference_rate_cache_key("EUR", "USD", source="frankfurter"), quote)
        resolver = GlobalFiatFxResolver(
            source=_Source(error=ConnectionError("frankfurter unreachable")),  # type: ignore[arg-type]
            cache=cache,
            config=GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600),
        )

        # Payments must not inherit catalog stale permission.
        with pytest.raises(FxUnavailableError):
            await resolver.get_rate("EUR", "USD")

        # The catalog path then serves the same bounded stale rate, flagged.
        resolution = await resolver.get_rate("EUR", "USD", allow_catalog_stale=True)
        assert resolution.stale is True
        assert resolution.rate == Decimal("1.10")

    async def test_stale_quote_beyond_the_stale_limit_is_never_used(self) -> None:
        quote = _quote("EUR", "USD", "1.10", age_seconds=7200, ttl_seconds=60)
        cache = build_fx_cache(backend="memory")
        await cache.put(reference_rate_cache_key("EUR", "USD", source="frankfurter"), quote)
        resolver = GlobalFiatFxResolver(
            source=_Source(error=ConnectionError("frankfurter unreachable")),  # type: ignore[arg-type]
            cache=cache,
            config=GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600),
        )

        with pytest.raises(FxUnavailableError):
            await resolver.get_catalog_rate("EUR", "USD")

    async def test_expired_live_quote_fails_closed_for_payments(self) -> None:
        resolver = _resolver(
            _Source(quote_factory=lambda b, q: _expired(b, q, age_seconds=600, ttl_seconds=60)),
            quote_ttl_seconds=60,
            max_stale_seconds=3600,
        )

        with pytest.raises(FxStaleError, match="expired"):
            await resolver.get_rate("EUR", "USD")

    async def test_expired_live_quote_is_cached_and_flagged_for_catalog(self) -> None:
        cache = build_fx_cache(backend="memory")
        resolver = GlobalFiatFxResolver(
            source=_Source(
                quote_factory=lambda b, q: _expired(b, q, age_seconds=600, ttl_seconds=60)
            ),  # type: ignore[arg-type]
            cache=cache,
            config=GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600),
        )

        resolution = await resolver.get_catalog_rate("EUR", "USD")

        assert resolution.stale is True
        assert (
            await cache.get(reference_rate_cache_key("EUR", "USD", source="frankfurter"))
            is not None
        )

    async def test_quote_older_than_the_live_freshness_limit_is_refused(self) -> None:
        resolver = _resolver(
            _Source(quote_factory=lambda b, q: _aged_live(b, q, age_seconds=7200)),
            quote_ttl_seconds=60,
            max_stale_seconds=3600,
        )

        with pytest.raises(FxStaleError, match="live freshness limit"):
            await resolver.get_catalog_rate("EUR", "USD")

    async def test_quote_older_than_the_quote_ttl_fails_closed_for_payments(self) -> None:
        resolver = _resolver(
            _Source(quote_factory=lambda b, q: _aged_live(b, q, age_seconds=600)),
            quote_ttl_seconds=60,
            max_stale_seconds=3600,
        )

        with pytest.raises(FxStaleError, match="quote TTL"):
            await resolver.get_rate("EUR", "USD")

    async def test_quote_older_than_the_quote_ttl_is_flagged_stale_for_catalog(self) -> None:
        resolver = _resolver(
            _Source(quote_factory=lambda b, q: _aged_live(b, q, age_seconds=600)),
            quote_ttl_seconds=60,
            max_stale_seconds=3600,
        )

        resolution = await resolver.get_catalog_rate("EUR", "USD")

        assert resolution.stale is True
        assert resolution.rate == Decimal("1.10")


class TestResolverProviderFailureContract:
    async def test_malformed_payload_is_never_hidden_behind_a_stale_rate(self) -> None:
        cache = build_fx_cache(backend="memory")
        await cache.put(
            reference_rate_cache_key("EUR", "USD", source="frankfurter"),
            _quote("EUR", "USD", "1.10", age_seconds=120, ttl_seconds=60),
        )
        resolver = GlobalFiatFxResolver(
            source=_Source(error=FxInvalidQuoteError("payload base mismatch")),  # type: ignore[arg-type]
            cache=cache,
            config=GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600),
        )

        with pytest.raises(FxInvalidQuoteError):
            await resolver.get_catalog_rate("EUR", "USD")

    async def test_fx_error_from_source_is_propagated_unchanged(self) -> None:
        resolver = _resolver(_Source(error=FxUnavailableError("upstream 503")))

        with pytest.raises(FxUnavailableError, match="upstream 503"):
            await resolver.get_catalog_rate("EUR", "USD")

    async def test_transport_failure_without_any_cache_fails_closed(self) -> None:
        source = _Source(error=ConnectionError("dns failure"))
        resolver = _resolver(source)

        with pytest.raises(FxUnavailableError, match="unavailable"):
            await resolver.get_catalog_rate("EUR", "USD")
        assert source.calls == [("EUR", "USD")]

    async def test_pair_mismatch_from_source_is_a_contract_defect(self) -> None:
        def _wrong_pair(base: str, quote: str) -> FxReferenceQuote:
            return _quote("GBP", quote, "1.27")

        resolver = _resolver(_Source(quote_factory=_wrong_pair))

        with pytest.raises(FxInvalidQuoteError, match="reference source returned GBP->USD"):
            await resolver.get_rate("EUR", "USD")

    async def test_source_market_provenance_mismatch_is_refused(self) -> None:
        resolver = _resolver(
            _Source(quote_factory=lambda b, q: _quote(b, q, "1.10", source_market="EUR/JPY"))
        )

        with pytest.raises(FxInvalidQuoteError, match="market provenance mismatch"):
            await resolver.get_rate("EUR", "USD")

    async def test_quote_source_provenance_mismatch_is_refused(self) -> None:
        resolver = _resolver(_Source(quote_factory=lambda b, q: _quote(b, q, "1.10", source="ecb")))

        with pytest.raises(FxInvalidQuoteError, match="source provenance mismatch"):
            await resolver.get_rate("EUR", "USD")


class TestResolverCacheHygiene:
    @pytest.mark.parametrize(
        "cached",
        [
            "not-a-quote",
            _quote("GBP", "USD", "1.27"),
            _quote("EUR", "USD", "1.10", source="ecb"),
            _quote("EUR", "USD", "1.10", source_market="EUR/JPY"),
        ],
    )
    async def test_unusable_cache_rows_are_ignored(self, cached: object) -> None:
        resolver = _resolver(_Source(), cache=_StubCache(cached))

        assert await resolver._safe_cache_get("k", "EUR", "USD") is None

    async def test_cache_read_failure_is_ignored_and_live_fetch_still_runs(self) -> None:
        source = _Source()
        resolver = _resolver(source, cache=_StubCache(raise_on_get=True))

        resolution = await resolver.get_rate("EUR", "USD")

        assert resolution.rate == Decimal("1.10")
        assert source.calls == [("EUR", "USD")]

    async def test_cache_write_failure_does_not_break_the_quote(self) -> None:
        class _PutFails(_StubCache):
            async def put(self, key: str, quote: object) -> None:
                raise RuntimeError("cache backend down")

        resolver = _resolver(_Source(), cache=_PutFails())

        resolution = await resolver.get_rate("EUR", "USD")

        assert resolution.stale is False
        assert resolution.rate == Decimal("1.10")


class TestResolverLifecycleAndCompatibilityApi:
    async def test_close_releases_source_and_tolerates_a_failing_cache(self) -> None:
        class _BadCache:
            async def close(self) -> None:
                raise RuntimeError("cache close failed")

        source = _Source()
        resolver = GlobalFiatFxResolver(
            source=source,  # type: ignore[arg-type]
            cache=_BadCache(),  # type: ignore[arg-type]
            config=GlobalFiatFxConfig(),
        )

        await resolver.close()

        assert source.closed == 1

    async def test_close_skips_resources_that_own_no_transport(self) -> None:
        source = _Source()
        resolver = GlobalFiatFxResolver(
            source=source,  # type: ignore[arg-type]
            cache=object(),  # type: ignore[arg-type]
            config=GlobalFiatFxConfig(),
        )

        await resolver.close()

        assert source.closed == 1

    async def test_resolve_identity_pair_never_calls_the_provider(self) -> None:
        source = _Source()
        resolver = _resolver(source)

        resolved = await resolver.resolve(1234, "USD", "USD", FxPurpose.DISPLAY)

        assert resolved.target_amount_minor == 1234
        assert resolved.target_currency == "USD"
        assert resolved.rate == Decimal(1)
        assert resolved.stale is False
        assert resolved.proxy is False
        assert source.calls == []

    async def test_resolve_converts_minor_units_through_the_reference_rate(self) -> None:
        resolver = _resolver(_Source(rate="1.10"))

        # EUR 10.00 -> USD 11.00
        resolved = await resolver.resolve(1000, "EUR", "USD", FxPurpose.CHARGE)

        assert resolved.target_amount_minor == 1100
        assert resolved.source == "frankfurter"
        assert resolved.path == "reference EUR/USD"
        assert resolved.stale is False

    async def test_resolve_accepts_a_plain_string_purpose(self) -> None:
        resolver = _resolver(_Source(rate="1.10"))

        resolved = await resolver.resolve(1000, "EUR", "USD", "charge")  # type: ignore[arg-type]

        assert resolved.purpose is FxPurpose.CHARGE

    async def test_resolve_honours_the_zero_exponent_of_jpy(self) -> None:
        resolver = _resolver(_Source(rate="0.0067"))

        # JPY has no minor unit: 691 minor IS 691 yen -> 4.6297 -> 463 cents.
        resolved = await resolver.resolve(691, "JPY", "USD", FxPurpose.DISPLAY)

        assert resolved.target_amount_minor == 463
        assert resolved.target_currency == "USD"

    @pytest.mark.parametrize("amount", [True, 0, -1, 1.5, 9_223_372_036_854_775_808])
    async def test_resolve_rejects_amounts_outside_positive_int64(self, amount: object) -> None:
        resolver = _resolver(_Source())

        with pytest.raises(ValueError, match="positive signed int64"):
            await resolver.resolve(amount, "EUR", "USD", FxPurpose.DISPLAY)  # type: ignore[arg-type]

    async def test_resolve_uses_the_payment_path_not_the_stale_catalog_path(self) -> None:
        cache = build_fx_cache(backend="memory")
        await cache.put(
            reference_rate_cache_key("EUR", "USD", source="frankfurter"),
            _quote("EUR", "USD", "9.99", age_seconds=120, ttl_seconds=60),
        )
        resolver = GlobalFiatFxResolver(
            source=_Source(error=ConnectionError("frankfurter unreachable")),  # type: ignore[arg-type]
            cache=cache,
            config=GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600),
        )

        with pytest.raises(FxUnavailableError):
            await resolver.resolve(1000, "EUR", "USD", FxPurpose.CHARGE)


def _expired(base: str, quote: str, *, age_seconds: int, ttl_seconds: int) -> FxReferenceQuote:
    """A live-quoted pair whose TTL already elapsed before it was returned."""
    return _quote(base, quote, "1.10", age_seconds=age_seconds, ttl_seconds=ttl_seconds)


def _aged_live(base: str, quote: str, *, age_seconds: int) -> FxReferenceQuote:
    """An unexpired quote that is older than the configured quote TTL."""
    return _quote(base, quote, "1.10", age_seconds=age_seconds, ttl_seconds=age_seconds * 2)
