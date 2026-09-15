"""FX cache: fresh hits, expiry refresh, bounded stale fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cloud_platform.modules.fx.cache import InMemoryFxCache, quote_from_document, quote_to_document
from cloud_platform.modules.fx.domain import FxMarketQuote, FxPurpose, FxUnavailableError
from cloud_platform.modules.fx.service import FxConfig, FxResolver


def _quote(age_seconds: float = 0, ttl: int = 60) -> FxMarketQuote:
    moment = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return FxMarketQuote(
        base_currency="EUR",
        quote_currency="IRT",
        buy_rate=Decimal("200000"),
        sell_rate=Decimal("199000"),
        source="abantether",
        source_market="EURIRT",
        observed_at=moment,
        expires_at=moment + timedelta(seconds=ttl),
    )


class _FlakySource:
    source_name = "abantether-test"

    def __init__(self, fresh: FxMarketQuote | None = None) -> None:
        self.fresh = fresh
        self.calls = 0

    async def get_quote(self, base: str, quote: str) -> FxMarketQuote:
        self.calls += 1
        if self.fresh is None:
            raise FxUnavailableError("provider down")
        return self.fresh

    async def close(self) -> None:
        return None


class TestCacheDocuments:
    def test_round_trip(self) -> None:
        quote = _quote()
        assert quote_from_document(quote_to_document(quote)) == quote

    def test_malformed_rejected(self) -> None:
        assert quote_from_document("not-json") is None
        assert quote_from_document("") is None
        assert quote_from_document(None) is None


class TestResolverCache:
    async def test_fresh_cache_hit_needs_no_fetch(self) -> None:
        cache = InMemoryFxCache()
        await cache.put("EUR->IRT", _quote(age_seconds=5))
        source = _FlakySource(fresh=_quote())
        resolver = FxResolver(source=source, cache=cache, config=FxConfig())
        out = await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)
        assert out.stale is False
        assert source.calls == 0

    async def test_expiry_triggers_refresh(self) -> None:
        cache = InMemoryFxCache()
        await cache.put("EUR->IRT", _quote(age_seconds=120, ttl=60))
        fresh = _quote(age_seconds=0)
        source = _FlakySource(fresh=fresh)
        resolver = FxResolver(source=source, cache=cache, config=FxConfig())
        out = await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)
        assert source.calls == 1
        assert out.stale is False

    async def test_bounded_stale_fallback_marks_stale(self) -> None:
        cache = InMemoryFxCache()
        await cache.put("EUR->IRT", _quote(age_seconds=120, ttl=60))  # 60s past expiry
        source = _FlakySource(fresh=None)  # provider down
        resolver = FxResolver(source=source, cache=cache, config=FxConfig())
        out = await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)
        assert out.stale is True
        assert out.target_amount_minor == 1_000_000  # last-known-good still converts

    async def test_max_stale_rejection_fails_closed(self) -> None:
        cache = InMemoryFxCache()
        await cache.put("EUR->IRT", _quote(age_seconds=600, ttl=60))  # way too old
        source = _FlakySource(fresh=None)
        resolver = FxResolver(source=source, cache=cache, config=FxConfig())
        with pytest.raises(FxUnavailableError):
            await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)

    async def test_charge_rejects_stale_display_would_allow(self) -> None:
        cache = InMemoryFxCache()
        # 60s stale: within display window (300s) but beyond charge window (30s).
        await cache.put("EUR->IRT", _quote(age_seconds=120, ttl=60))
        source = _FlakySource(fresh=None)
        resolver = FxResolver(source=source, cache=cache, config=FxConfig())
        display = await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)
        assert display.stale is True
        with pytest.raises(FxUnavailableError):
            await resolver.resolve(500, "EUR", "IRT", FxPurpose.CHARGE)

    async def test_provider_recovery_replaces_stale(self) -> None:
        cache = InMemoryFxCache()
        await cache.put("EUR->IRT", _quote(age_seconds=120, ttl=60))
        source = _FlakySource(fresh=None)
        resolver = FxResolver(source=source, cache=cache, config=FxConfig())
        stale = await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)
        assert stale.stale is True
        source.fresh = _quote(age_seconds=0)
        fresh = await resolver.resolve(500, "EUR", "IRT", FxPurpose.DISPLAY)
        assert fresh.stale is False
        assert source.calls == 2
