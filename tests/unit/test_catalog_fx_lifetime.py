"""Catalog FX publication horizon: the "empty storefront" regression.

2026-09-26 production incident (Leaseweb hourly market, 507 stored offers):

    02:02  the GBP->USD reference quote is observed (3600s TTL, expires 03:02)
    03:00  the catalog sync starts; the cached quote is STILL "fresh" (2 min
           left), so it is used for every row of the run as ``fx_stale=false``
           with ``catalog_valid_until = 03:02``
    03:02  the run's own prices cross their validity bound and the whole
           foreign market silently disappears from the storefront
    03:15  the next sync refreshes the quote and the market comes back

Every provider read was healthy. The reference rate simply did not live long
enough to survive one refresh cycle, and nothing forced it to.

These tests replay that timeline with a patchable clock and pin the fix: a
NEWLY published catalog price must stay provable until the next scheduled
refresh has completed, or it is not published at all (the row keeps its
previous, still-valid price and the run records why).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.core.config import load_settings
from cloud_platform.modules.fx import service as fx_service
from cloud_platform.modules.fx.cache import build_fx_cache
from cloud_platform.modules.fx.domain import (
    FxError,
    FxReferenceQuote,
    FxUnavailableError,
)
from cloud_platform.modules.fx.ports import reference_rate_cache_key
from cloud_platform.modules.fx.service import (
    GlobalFiatFxConfig,
    GlobalFiatFxResolver,
    ReferenceRateResolution,
)
from cloud_platform.modules.offers import auto_sync as auto_sync_module
from cloud_platform.modules.offers import domain as offers_domain
from cloud_platform.modules.offers import pricing as pricing_module
from cloud_platform.modules.offers.auto_sync import CatalogAutoSyncCoordinator
from cloud_platform.modules.offers.domain import (
    CatalogSyncReport,
    PricingPolicy,
    SellableOffer,
    has_valid_pricing_provenance,
)
from cloud_platform.modules.offers.pricing import CatalogOfferPricer, OfferPricingError

#: The incident's configuration: 900s cadence, 600s job budget, 300s margin.
SYNC_INTERVAL_SECONDS = 900
SYNC_TIMEOUT_SECONDS = 600
SAFETY_MARGIN_SECONDS = 300
HORIZON_SECONDS = SYNC_INTERVAL_SECONDS + SYNC_TIMEOUT_SECONDS + SAFETY_MARGIN_SECONDS

QUOTE_TTL_SECONDS = 3600
CATALOG_STALE_LIMIT_SECONDS = 86400

PROVIDER = "leaseweb"
PRODUCT_ID = "lsw.m4.large"
LOCATION_ID = "eu-central-1"
ACCOUNT_ID = "sales-org-north"


class _ClockMeta(type):
    """Keeps ``isinstance(value, datetime)`` honest while the clock is patched.

    The modules under test are patched by rebinding their ``datetime`` name to
    the frozen class below, so their ``isinstance(quote.observed_at, datetime)``
    guards would otherwise reject every REAL datetime instance.
    """

    def __instancecheck__(cls, instance: object) -> bool:
        return isinstance(instance, datetime)


class _FrozenClock(datetime, metaclass=_ClockMeta):
    """``datetime`` subclass whose ``now()`` is test-controlled.

    A subclass (never a mock) so ``datetime.min``/``combine``/``fromisoformat``
    keep working inside the code under test.
    """

    current: ClassVar[datetime] = datetime(2020, 1, 1, 2, 2, tzinfo=UTC)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        moment = cls.current
        if tz is None:
            return moment.replace(tzinfo=None)
        return moment.astimezone(tz)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Freeze every catalog-path clock a day in the past, at 02:02.

    Anchoring in the past keeps the absolute-time guards elsewhere honest (a
    quote may never be observed in the future) while still replaying the whole
    02:02 -> 03:15 timeline in microseconds.
    """
    anchor = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(days=1)
    frozen = type(
        "FrozenClock",
        (_FrozenClock,),
        {"current": anchor.replace(hour=2, minute=2)},
    )
    for module in (fx_service, pricing_module, offers_domain, auto_sync_module):
        monkeypatch.setattr(module, "datetime", frozen)
    return frozen


def _at(clock: Any, hour: int, minute: int, second: int = 0) -> None:
    """Move the frozen clock (same day) to ``hour:minute:second``."""
    clock.current = clock.current.replace(hour=hour, minute=minute, second=second)


def _quote(
    base: str = "GBP",
    quote: str = "USD",
    *,
    rate: str = "1.31",
    observed_at: datetime,
    ttl_seconds: int = QUOTE_TTL_SECONDS,
) -> FxReferenceQuote:
    return FxReferenceQuote(
        base_currency=base,
        quote_currency=quote,
        rate=Decimal(rate),
        source="frankfurter",
        source_market=f"{base}/{quote}",
        provider_date=observed_at.date(),
        observed_at=observed_at,
        expires_at=observed_at + timedelta(seconds=ttl_seconds),
    )


async def _seed(cache: Any, quote: FxReferenceQuote, *, source: str = "frankfurter") -> None:
    key = reference_rate_cache_key(quote.base_currency, quote.quote_currency, source=source)
    await cache.put(key, quote)


class _ReferenceSource:
    """Deterministic Frankfurter-shaped source that counts every call."""

    source_name = "frankfurter"

    def __init__(
        self,
        clock: Any,
        *,
        rate: str = "1.30",
        error: Exception | None = None,
        ttl_seconds: int = QUOTE_TTL_SECONDS,
    ) -> None:
        self.clock = clock
        self.rate = rate
        self.error = error
        self.ttl_seconds = ttl_seconds
        self.calls: list[tuple[str, str]] = []

    async def get_reference_rate(self, base: str, quote: str) -> FxReferenceQuote:
        self.calls.append((base, quote))
        if self.error is not None:
            raise self.error
        # Observed at the FROZEN now, exactly like a real provider quote.
        return _quote(
            base,
            quote,
            rate=self.rate,
            observed_at=self.clock.now(UTC),
            ttl_seconds=self.ttl_seconds,
        )


def _resolver(
    source: _ReferenceSource,
    cache: Any,
    *,
    catalog_stale_limit_seconds: int = CATALOG_STALE_LIMIT_SECONDS,
) -> GlobalFiatFxResolver:
    return GlobalFiatFxResolver(
        source=source,  # type: ignore[arg-type]
        cache=cache,
        config=GlobalFiatFxConfig(
            quote_ttl_seconds=QUOTE_TTL_SECONDS,
            max_stale_seconds=345600,
            catalog_max_stale_seconds=catalog_stale_limit_seconds,
        ),
    )


def _hourly_offer() -> SellableOffer:
    """One Leaseweb Cloud offer: 0.05 GBP/hour, canonical USD storefront price."""
    return SellableOffer(
        id=uuid4(),
        provider_key=PROVIDER,
        product_id=PRODUCT_ID,
        location_id=LOCATION_ID,
        name=PRODUCT_ID,
        vcpu=4,
        ram_gb=8,
        disk_gb=5,
        traffic=None,
        provider_cost_minor=5,
        provider_cost_currency="GBP",
        selling_price_minor=0,
        selling_currency="USD",
        billing_parameters={"provider_hourly_rate": "0.05"},
        billing_model="hourly",
        provider_available=True,
        enabled=False,
        auto_priced=True,
        provider_account_id=ACCOUNT_ID,
    )


def _policy() -> PricingPolicy:
    return PricingPolicy(mode="markup", markup_percent=25, auto_publish=True)


def _publishable(
    offer: SellableOffer, priced: Any, *, stale_limit: int = CATALOG_STALE_LIMIT_SECONDS
) -> SellableOffer:
    """The row as the storefront gate would see it after this price is written."""
    candidate = replace(
        offer,
        selling_price_minor=priced.selling_price_minor,
        selling_currency=priced.selling_currency,
        pricing_metadata=dict(priced.pricing_metadata),
    )
    return candidate


def _valid(candidate: SellableOffer, *, stale_limit: int = CATALOG_STALE_LIMIT_SECONDS) -> bool:
    return has_valid_pricing_provenance(candidate, "USD", catalog_stale_limit_seconds=stale_limit)


def _valid_until(metadata: dict[str, object]) -> datetime:
    return datetime.fromisoformat(str(metadata["catalog_valid_until"]))


class TestIncidentReproduction:
    """The exact predicate that emptied the storefront on 2026-09-26."""

    async def test_without_a_horizon_the_near_expiry_quote_is_published_as_fresh(
        self, clock: Any
    ) -> None:
        """Pre-fix behavior: 2 minutes of TTL left is still reported as 'fresh'."""
        cache = build_fx_cache(backend="memory")
        observed = clock.current  # 02:02
        await _seed(cache, _quote(observed_at=observed))
        source = _ReferenceSource(clock)
        resolver = _resolver(source, cache)

        _at(clock, 3, 0)  # the 03:00 run
        resolution = await resolver.get_catalog_rate("GBP", "USD")

        assert resolution.stale is False
        assert resolution.observed_at == observed
        assert resolution.quote.expires_at - clock.current == timedelta(seconds=120)
        assert source.calls == []  # the cached quote was reused without a refresh

        # Written exactly as the incident did it: valid for two more minutes.
        pricer = CatalogOfferPricer(resolver, "USD")
        priced = await pricer.price_auto(_hourly_offer(), _policy())
        assert priced.pricing_metadata["fx_stale"] is False
        assert _valid_until(priced.pricing_metadata) - clock.current == timedelta(seconds=120)

        # ... and 10 minutes later the whole market is gone, with no provider
        # error anywhere: only the reference rate ran out.
        _at(clock, 3, 10)
        assert _valid(_publishable(_hourly_offer(), priced)) is False

    async def test_near_expiry_quote_is_refreshed_instead_of_published(self, clock: Any) -> None:
        """Fix, part 1: a quote too short for the horizon is not 'fresh'."""
        cache = build_fx_cache(backend="memory")
        await _seed(cache, _quote(observed_at=clock.current))  # 02:02 -> 03:02
        source = _ReferenceSource(clock)
        resolver = _resolver(source, cache)

        _at(clock, 3, 0)
        resolution = await resolver.get_catalog_rate(
            "GBP", "USD", min_remaining_lifetime_seconds=HORIZON_SECONDS
        )

        assert source.calls == [("GBP", "USD")]  # a real refresh happened
        assert resolution.stale is False
        assert resolution.observed_at == clock.current
        assert resolution.quote.expires_at - clock.current >= timedelta(seconds=HORIZON_SECONDS)

    async def test_failed_refresh_falls_back_to_the_bounded_stale_window(self, clock: Any) -> None:
        """Fix, part 2: provider down + observation inside the stale window.

        The row is repriced through the explicitly audited bounded policy, is
        still there an hour later, and never claims to be ``fx_stale=false``.
        """
        cache = build_fx_cache(backend="memory")
        observed = clock.current  # 02:02
        await _seed(cache, _quote(observed_at=observed))
        source = _ReferenceSource(clock, error=FxUnavailableError("frankfurter is down"))
        resolver = _resolver(source, cache)

        _at(clock, 3, 0)
        pricer = CatalogOfferPricer(
            resolver,
            "USD",
            min_catalog_fresh_lifetime_seconds=HORIZON_SECONDS,
            catalog_horizon_anchor=clock.current,
        )
        offer = _hourly_offer()
        priced = await pricer.price_auto(offer, _policy())

        assert priced.pricing_metadata["fx_stale"] is True
        assert priced.pricing_metadata["fx_stale_limit_seconds"] == CATALOG_STALE_LIMIT_SECONDS
        expected_until = observed + timedelta(seconds=CATALOG_STALE_LIMIT_SECONDS)
        assert _valid_until(priced.pricing_metadata) == expected_until
        assert _publishable(offer, priced).selling_price_minor > 0
        assert _valid(_publishable(offer, priced)) is True

        # The original quote's own TTL (03:02) has long passed, and the next
        # sync has not even started: the offer is still sellable.
        _at(clock, 3, 10)
        assert _valid(_publishable(offer, priced)) is True
        _at(clock, 4, 0)
        assert _valid(_publishable(offer, priced)) is True
        assert source.calls == [("GBP", "USD")]

    async def test_prices_written_by_a_long_walk_outlive_the_next_refresh(self, clock: Any) -> None:
        """A 2-3 minute provider walk must not shorten what it publishes.

        The run's first row is written at 03:00 and the last one at 03:02:40 —
        after the 02:02 quote's own expiry — yet every row stays provable until
        the next refresh could possibly have replaced it, and the pair was
        fetched exactly once for all of them.
        """
        cache = build_fx_cache(backend="memory")
        await _seed(cache, _quote(observed_at=clock.current))  # 02:02 -> 03:02
        source = _ReferenceSource(clock)
        resolver = _resolver(source, cache)

        _at(clock, 3, 0)
        anchor = clock.current
        pricer = CatalogOfferPricer(
            resolver,
            "USD",
            min_catalog_fresh_lifetime_seconds=HORIZON_SECONDS,
            catalog_horizon_anchor=anchor,
        )
        published: list[SellableOffer] = []
        for index in range(3):
            offer = _hourly_offer()
            priced = await pricer.price_auto(offer, _policy())
            published.append(_publishable(offer, priced))
            if index < 2:  # 03:00 -> 03:01:20 -> 03:02:40 (a real 160s walk)
                clock.current = clock.current + timedelta(seconds=80)

        next_refresh_completes = anchor + timedelta(
            seconds=SYNC_INTERVAL_SECONDS + SYNC_TIMEOUT_SECONDS
        )
        for row in published:
            assert _valid_until(row.pricing_metadata) >= next_refresh_completes
            assert _valid(row) is True
        # The run's own start anchor decides, not each row's write instant.
        assert source.calls == [("GBP", "USD")]

        _at(clock, 3, 15)  # the next sync is due but has not repriced anything
        assert all(_valid(row) is True for row in published)

    async def test_pricer_refuses_a_resolution_that_cannot_cover_the_next_refresh(
        self, clock: Any
    ) -> None:
        """The write-time gate: an old-style short resolution is refused.

        A resolver that cannot honour the horizon (legacy double, direct
        construction) still cannot get a short-lived price into the catalog.
        """
        short = ReferenceRateResolution(
            _quote(
                observed_at=clock.current - timedelta(seconds=QUOTE_TTL_SECONDS - 60),
                ttl_seconds=QUOTE_TTL_SECONDS,
            ),
            stale=False,
        )

        class _LegacyRates:
            async def get_rate(
                self, base: str, quote: str, *, allow_catalog_stale: bool = False
            ) -> ReferenceRateResolution:
                return short

        pricer = CatalogOfferPricer(
            _LegacyRates(),  # type: ignore[arg-type]
            "USD",
            min_catalog_fresh_lifetime_seconds=HORIZON_SECONDS,
            catalog_horizon_anchor=clock.current,
        )
        with pytest.raises(OfferPricingError, match="next scheduled catalog refresh"):
            await pricer.price_auto(_hourly_offer(), _policy())

    async def test_a_stale_window_past_its_limit_fails_closed(self, clock: Any) -> None:
        """Beyond the configured catalog stale limit nothing is published."""
        cache = build_fx_cache(backend="memory")
        observed = clock.current - timedelta(seconds=4000)  # age > 3600s limit
        await _seed(cache, _quote(observed_at=observed, ttl_seconds=QUOTE_TTL_SECONDS))
        source = _ReferenceSource(clock, error=FxUnavailableError("frankfurter is down"))
        resolver = _resolver(source, cache, catalog_stale_limit_seconds=QUOTE_TTL_SECONDS)

        with pytest.raises(FxError):
            await resolver.get_catalog_rate(
                "GBP", "USD", min_remaining_lifetime_seconds=HORIZON_SECONDS
            )

    async def test_a_published_stale_price_stops_selling_at_its_own_bound(self, clock: Any) -> None:
        """The bounded window is a real bound, not a licence to price forever."""
        cache = build_fx_cache(backend="memory")
        observed = clock.current  # 02:02
        await _seed(cache, _quote(observed_at=observed))
        resolver = _resolver(
            _ReferenceSource(clock, error=FxUnavailableError("frankfurter is down")), cache
        )
        _at(clock, 3, 0)
        offer = _hourly_offer()
        priced = await CatalogOfferPricer(
            resolver,
            "USD",
            min_catalog_fresh_lifetime_seconds=HORIZON_SECONDS,
            catalog_horizon_anchor=clock.current,
        ).price_auto(offer, _policy())
        row = _publishable(offer, priced)
        bound = observed + timedelta(seconds=CATALOG_STALE_LIMIT_SECONDS)
        assert _valid_until(row.pricing_metadata) == bound
        assert _valid(row) is True

        # One second past the bound the customer no longer sees a price that
        # nobody can prove; the row itself is retained for the next reprice.
        clock.current = bound + timedelta(seconds=1)
        assert _valid(row) is False


def _report() -> CatalogSyncReport:
    return CatalogSyncReport(
        provider_key=PROVIDER,
        ok=True,
        complete=True,
        discovered=1,
        persisted=1,
        verified={(PRODUCT_ID, LOCATION_ID)},
        billing_model="hourly",
    )


class _Source:
    provider_key = PROVIDER

    def __init__(self, report: CatalogSyncReport) -> None:
        self._report = report

    async def sync_catalog(self) -> CatalogSyncReport:
        return self._report


def _coordinator(
    *,
    rates: Any,
    offer: SellableOffer,
    sellable_counts: list[int],
    horizon: int = HORIZON_SECONDS,
) -> Any:
    repo = MagicMock()
    repo.get_by_ref = AsyncMock(return_value=offer)
    repo.set_auto_price_if_current = AsyncMock(return_value=offer)
    repo.publish_if_current = AsyncMock(return_value=offer)
    repo.record_auto_pricing_failure_if_current = AsyncMock(return_value=offer)
    rows = [offer] * max(sellable_counts) if sellable_counts else []
    repo.list_sellable = AsyncMock(side_effect=[[offer] * count for count in sellable_counts])
    state = MagicMock()
    state.record_run = AsyncMock(return_value=None)
    lock = MagicMock()
    coordinator = CatalogAutoSyncCoordinator(
        sources=[_Source(_report())],
        offers=repo,
        state=state,
        lock=lock,
        pricing_policies={PROVIDER: _policy()},
        reference_rates=rates,
        catalog_currency="USD",
        identity_ttl_seconds=QUOTE_TTL_SECONDS,
        catalog_min_fresh_lifetime_seconds=horizon,
    )
    del rows
    return coordinator, repo


class TestCoordinatorPublication:
    """The coordinator must publish provable prices and say so when it cannot."""

    async def test_coordinator_publishes_horizon_bounded_fx(self, clock: Any) -> None:
        cache = build_fx_cache(backend="memory")
        observed = clock.current
        await _seed(cache, _quote(observed_at=observed))  # 02:02 -> 03:02
        source = _ReferenceSource(clock, error=FxUnavailableError("frankfurter is down"))
        resolver = _resolver(source, cache)
        offer = _hourly_offer()
        coordinator, repo = _coordinator(rates=resolver, offer=offer, sellable_counts=[1, 1])

        _at(clock, 3, 0)
        report = await coordinator.run()

        provider_report = report.providers[0]
        assert provider_report.previous_sellable == 1
        assert provider_report.resulting_sellable == 1
        assert provider_report.fx_observations[0].pair == "GBP/USD"
        assert provider_report.fx_observations[0].stale is True
        assert (
            provider_report.fx_observations[0].remaining_lifetime_at_sync_start_seconds
            >= HORIZON_SECONDS
        )
        prevented = [
            warning
            for warning in provider_report.warnings
            if warning.startswith("storefront blackout prevented")
        ]
        assert len(prevented) == 1
        assert "reason=bounded_catalog_fx" in prevented[0]
        assert "pair=GBP/USD" in prevented[0]
        assert "previous_sellable=1" in prevented[0]
        assert "resulting_sellable=1" in prevented[0]
        assert not any(
            warning.startswith("storefront blackout occurred")
            for warning in provider_report.warnings
        )

        metadata = repo.set_auto_price_if_current.await_args.kwargs["pricing_metadata"]
        assert metadata["fx_stale"] is True
        assert metadata["catalog_fresh_lifetime_required_seconds"] == HORIZON_SECONDS
        anchor = provider_report.sync_started_at
        assert anchor is not None
        assert _valid_until(metadata) >= anchor + timedelta(seconds=HORIZON_SECONDS)

    async def test_coordinator_reports_a_storefront_blackout_when_fx_cannot_prove(
        self, clock: Any
    ) -> None:
        """No legal rate anywhere: fail closed AND say exactly why."""
        cache = build_fx_cache(backend="memory")
        # Observation already outside the stale limit and the provider is down.
        await _seed(
            cache,
            _quote(observed_at=clock.current - timedelta(seconds=CATALOG_STALE_LIMIT_SECONDS + 60)),
        )
        resolver = _resolver(
            _ReferenceSource(clock, error=FxUnavailableError("frankfurter is down")),
            cache,
            catalog_stale_limit_seconds=CATALOG_STALE_LIMIT_SECONDS,
        )
        coordinator, _ = _coordinator(rates=resolver, offer=_hourly_offer(), sellable_counts=[7, 0])

        report = await coordinator.run()

        provider_report = report.providers[0]
        assert provider_report.previous_sellable == 7
        assert provider_report.resulting_sellable == 0
        blackouts = [
            warning
            for warning in provider_report.warnings
            if warning.startswith("storefront blackout occurred")
        ]
        assert len(blackouts) == 1
        assert "reason=fx_provenance_expired" in blackouts[0]
        assert "pair=GBP/USD" in blackouts[0]
        assert "previous_sellable=7" in blackouts[0]
        assert "resulting_sellable=0" in blackouts[0]


class TestHorizonConfiguration:
    """The horizon is derived from the sync cadence, never hardcoded."""

    def test_horizon_is_derived_from_the_catalog_cadence(self) -> None:
        settings = load_settings(None)
        assert settings.catalog_fx_min_remaining_lifetime_seconds == (
            settings.storefront_catalog_sync_interval_seconds
            + settings.storefront_catalog_sync_timeout_seconds
            + settings.storefront_catalog_fx_safety_margin_seconds
        )
        assert settings.catalog_fx_min_remaining_lifetime_seconds == HORIZON_SECONDS
        # The invariant the fix exists for: a fresh quote must be able to satisfy
        # it, otherwise every catalog price silently degrades to bounded-stale.
        assert (
            settings.catalog_fx_min_remaining_lifetime_seconds
            < settings.fx_frankfurter_quote_ttl_seconds
        )

    def test_a_timeout_above_the_interval_cannot_outlive_the_cadence(self) -> None:
        settings = load_settings(None).model_copy(
            update={"storefront_catalog_sync_timeout_seconds": 5000}
        )
        assert settings.catalog_fx_min_remaining_lifetime_seconds == (
            settings.storefront_catalog_sync_interval_seconds
            + settings.storefront_catalog_sync_interval_seconds
            + settings.storefront_catalog_fx_safety_margin_seconds
        )
