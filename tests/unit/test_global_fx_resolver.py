"""Deterministic GlobalFiatFxResolver contract: cache, singleflight, stale, memo.

No network, no float, no time-travel: the fake source counts every call and
quotes carry explicit ``observed_at``/``expires_at`` datetimes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from cloud_platform.modules.fx.cache import build_fx_cache
from cloud_platform.modules.fx.domain import (
    FxPurpose,
    FxReferenceQuote,
    FxUnavailableError,
    major_to_minor,
    minor_to_major,
)
from cloud_platform.modules.fx.ports import reference_rate_cache_key
from cloud_platform.modules.fx.service import (
    GlobalFiatFxConfig,
    GlobalFiatFxResolver,
)
from cloud_platform.modules.offers.domain import PricingPolicy, SellableOffer
from cloud_platform.modules.offers.pricing import CatalogOfferPricer


class _FakeReferenceSource:
    """Deterministic reference-rate source that counts every call."""

    source_name = "frankfurter"

    def __init__(
        self,
        rates: dict[tuple[str, str], Decimal] | None = None,
        *,
        error: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._rates = dict(rates or {})
        self._error = error
        self._delay = delay_seconds
        self.calls: list[tuple[str, str]] = []

    async def get_reference_rate(self, base: str, quote: str) -> FxReferenceQuote:
        self.calls.append((base, quote))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        now = datetime.now(UTC)
        return FxReferenceQuote(
            base_currency=base,
            quote_currency=quote,
            rate=self._rates[(base, quote)],
            source="frankfurter",
            source_market=f"{base}/{quote}",
            provider_date=now.date(),
            observed_at=now,
            expires_at=now + timedelta(seconds=3600),
        )


def _resolver(
    source: _FakeReferenceSource,
    *,
    quote_ttl_seconds: int = 3600,
    max_stale_seconds: int = 345600,
) -> GlobalFiatFxResolver:
    return GlobalFiatFxResolver(
        source=source,  # type: ignore[arg-type]
        cache=build_fx_cache(backend="memory"),
        config=GlobalFiatFxConfig(
            quote_ttl_seconds=quote_ttl_seconds,
            max_stale_seconds=max_stale_seconds,
        ),
    )


def _monthly_offer(
    cost_minor: int,
    cost_currency: str,
    exact_major_text: str,
    *,
    product_id: str = "plan",
) -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key="leaseweb",
        product_id=product_id,
        location_id="eu-west",
        name="Plan",
        vcpu=1,
        ram_gb=1,
        disk_gb=10,
        traffic=None,
        provider_cost_minor=cost_minor,
        provider_cost_currency=cost_currency,
        selling_price_minor=0,
        selling_currency=cost_currency,
        billing_parameters={"provider_monthly_rate": exact_major_text},
        billing_model="prepaid_monthly_fixed",
        provider_available=True,
        enabled=False,
        auto_priced=True,
    )


def _seed_expired_quote(
    base: str,
    quote: str,
    rate: Decimal,
    *,
    age_seconds: int,
    ttl_seconds: int,
) -> FxReferenceQuote:
    """Store an expired-but-bounded quote directly (no clock manipulation)."""
    observed_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    seeded = FxReferenceQuote(
        base_currency=base,
        quote_currency=quote,
        rate=rate,
        source="frankfurter",
        source_market=f"{base}/{quote}",
        provider_date=observed_at.date(),
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=ttl_seconds),
    )
    assert datetime.now(UTC) >= seeded.expires_at  # expired, so live fetch is attempted
    return seeded


async def test_fresh_fetch_then_cache_hit() -> None:
    source = _FakeReferenceSource({("EUR", "USD"): Decimal("1.10")})
    resolver = _resolver(source)

    first = await resolver.get_rate("EUR", "USD")
    second = await resolver.get_rate("EUR", "USD")

    assert source.calls == [("EUR", "USD")]
    assert first.stale is False
    assert second.stale is False
    assert first.rate == second.rate == Decimal("1.10")


async def test_source_calls_scale_with_distinct_pairs_not_offers() -> None:
    source = _FakeReferenceSource(
        {("EUR", "USD"): Decimal("1.10"), ("GBP", "USD"): Decimal("1.27")}
    )
    resolver = _resolver(source)
    pricer = CatalogOfferPricer(resolver, "USD")
    policy = PricingPolicy(markup_percent=10)
    offers = [
        _monthly_offer(1000, "EUR", "10.00", product_id="plan-a"),
        _monthly_offer(1000, "EUR", "10.00", product_id="plan-b"),
        _monthly_offer(2000, "GBP", "20.00", product_id="plan-c"),
    ]

    priced = [await pricer.price_auto(offer, policy) for offer in offers]

    # 10.00 * 1.10 * 1.10 = 12.10 -> 1210; 20.00 * 1.27 * 1.10 = 27.94 -> 2794.
    assert [p.selling_price_minor for p in priced] == [1210, 1210, 2794]
    assert sorted(set(source.calls)) == [("EUR", "USD"), ("GBP", "USD")]
    assert len(source.calls) == 2


async def test_concurrent_singleflight_fetches_source_once() -> None:
    source = _FakeReferenceSource({("EUR", "USD"): Decimal("1.10")}, delay_seconds=0.05)
    resolver = _resolver(source)

    resolutions = await asyncio.gather(*(resolver.get_rate("EUR", "USD") for _ in range(8)))

    assert len(source.calls) == 1
    assert all(resolution.rate == Decimal("1.10") for resolution in resolutions)
    assert all(resolution.stale is False for resolution in resolutions)


async def test_catalog_stale_fallback_when_live_fetch_fails() -> None:
    cache = build_fx_cache(backend="memory")
    seeded = _seed_expired_quote("EUR", "USD", Decimal("1.10"), age_seconds=120, ttl_seconds=60)
    await cache.put(reference_rate_cache_key("EUR", "USD", source="frankfurter"), seeded)
    resolver = GlobalFiatFxResolver(
        source=_FakeReferenceSource(error=ConnectionError("boom")),  # type: ignore[arg-type]
        cache=cache,
        config=GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600),
    )

    resolution = await resolver.get_catalog_rate("EUR", "USD")

    assert resolution.stale is True
    assert resolution.rate == Decimal("1.10")
    assert len(resolver.source.calls) == 1  # type: ignore[union-attr]
    assert resolver.catalog_stale_limit == 3600


async def test_payment_path_fails_closed_without_catalog_stale() -> None:
    cache = build_fx_cache(backend="memory")
    seeded = _seed_expired_quote("EUR", "USD", Decimal("1.10"), age_seconds=120, ttl_seconds=60)
    await cache.put(reference_rate_cache_key("EUR", "USD", source="frankfurter"), seeded)
    resolver = GlobalFiatFxResolver(
        source=_FakeReferenceSource(error=ConnectionError("boom")),  # type: ignore[arg-type]
        cache=cache,
        config=GlobalFiatFxConfig(quote_ttl_seconds=60, max_stale_seconds=3600),
    )

    try:
        await resolver.get_rate("EUR", "USD", allow_catalog_stale=False)
    except FxUnavailableError:
        pass
    else:
        raise AssertionError("payment path must fail closed on an expired quote")
    assert len(resolver.source.calls) == 1  # type: ignore[union-attr]


async def test_failure_memo_suppresses_repeated_source_calls() -> None:
    source = _FakeReferenceSource(error=ConnectionError("boom"))
    resolver = _resolver(source)

    for _ in range(3):
        try:
            await resolver.get_catalog_rate("EUR", "USD")
        except FxUnavailableError:
            pass
        else:
            raise AssertionError("empty cache plus source outage must raise")

    assert len(source.calls) == 1


async def test_identity_pair_never_touches_source() -> None:
    source = _FakeReferenceSource({("EUR", "USD"): Decimal("1.10")})
    resolver = _resolver(source)

    resolution = await resolver.get_rate("USD", "USD")

    assert resolution.rate == Decimal(1)
    assert resolution.stale is False
    assert source.calls == []


async def test_jpy_zero_decimal_round_trip() -> None:
    assert minor_to_major(691, "JPY") == Decimal(691)
    assert major_to_minor(Decimal("691"), "JPY", FxPurpose.CHARGE) == 691


async def test_jpy_offer_converts_without_phantom_cents() -> None:
    source = _FakeReferenceSource({("JPY", "USD"): Decimal("0.0067")})
    resolver = _resolver(source)

    # JPY 691 minor IS ¥691 (exponent 0); 691 * 0.0067 = 4.6297 -> 463 cents.
    priced = await CatalogOfferPricer(resolver, "USD").price_auto(
        _monthly_offer(691, "JPY", "691"), PricingPolicy(markup_percent=0)
    )

    assert priced.selling_price_minor == 463
    assert priced.selling_currency == "USD"
    assert str(priced.pricing_metadata["source_amount"]) == "691"
    assert len(source.calls) == 1
