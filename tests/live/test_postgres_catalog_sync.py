"""Opt-in live tests against a REAL PostgreSQL (advisory-lock contract).

These tests are skipped unless::

    CLOUD_PLATFORM_TEST_POSTGRES_URL=postgresql+asyncpg://... (a SCRATCH database)

They exist to prove what mocks cannot — the PostgreSQL BIGINT contract:

- the deterministic catalog-sync lock key binds as a signed int64
  (asyncpg validates the bound client-side; the old unsigned derivation
  raised ``OverflowError``/``DataError`` and blocked every auto-sync);
- two backends serialize on the same key (first True, second False);
- the coordinator runs PAST lock acquisition;
- a normal Leaseweb catalog sync repairs stale ``ProviderLocation`` rows
  (production pin: FRA-10/FRA-14/LON-11/LON-12 with NULL metadata) and the
  storefront groups them into ``🇩🇪 Frankfurt`` / ``🇬🇧 London``.

The scratch database is created/dropped table-wise per test (never touched
otherwise). No provider network is used: the Leaseweb adapter is scripted
exactly like the unit backfill tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cloud_platform.modules.catalog.domain import LocationRecord
from cloud_platform.modules.catalog.repository import (
    _CATALOG_SYNC_LOCK_KEY,
    PostgresAdvisoryCatalogSyncLock,
    SqlAlchemyLocationRepository,
)
from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY
from cloud_platform.modules.offers.repository import (
    SqlAlchemyCatalogSyncStateRepository,
    SqlAlchemySellableOfferRepository,
)
from cloud_platform.providers.leaseweb.ordering import LocationEligibility, LocationProbe

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()

requires_postgres = pytest.mark.skipif(
    not DB_URL,
    reason="live PostgreSQL tests require CLOUD_PLATFORM_TEST_POSTGRES_URL",
)

PROVIDER = "leaseweb"
SIGNING_KEY = "postgres-catalog-sync-key"

SIX = (
    ("FRA-01", "Frankfurt", "DE"),
    ("FRA-10", "Frankfurt", "DE"),
    ("FRA-14", "Frankfurt", "DE"),
    ("LON-01", "London", "GB"),
    ("LON-11", "London", "GB"),
    ("LON-12", "London", "GB"),
)

STALE = ("FRA-10", "FRA-14", "LON-11", "LON-12")


class _Product:
    def __init__(self, location: str) -> None:
        self.id = "VPS02_1"
        self.name = "Leaseweb VPS 1"
        self.location = location
        self.vcpu = 4
        self.ram_gb = 6
        self.disk_gb = 100
        self.traffic = "5 TB"
        self.currency = "EUR"
        self.monthly_price_minor = 624
        # Exact provider text the catalog pricer requires (never rebuilt
        # from rounded minor units).
        self.monthly_price_exact = "6.24"


class _Detail:
    def __init__(self, location: str) -> None:
        self.product = _Product(location)
        self.available_locations = (location,)
        self.os_options = ()
        self.control_panels = ()


class _OrderingProvider:
    """One credential account serving all six halls (scripted probes)."""

    discovery_seeds: tuple[str, ...] = ()
    _contract_term = "1_MONTH"
    _billing_cycle = "1_MONTH"

    def __init__(self, locations: tuple[str, ...]) -> None:
        self._locations = locations

    async def list_locations(self) -> list[Any]:
        return []

    def describe_location(self, code: str) -> Any:
        from cloud_platform.providers.leaseweb.ordering import (
            LeaseWebOrderingProvider,
        )

        return LeaseWebOrderingProvider.describe_location(
            LeaseWebOrderingProvider.__new__(LeaseWebOrderingProvider), code
        )

    async def list_products_unscoped(self) -> list[Any]:
        return [_Product(location) for location in ("LON-11", "LON-12")]

    async def probe_location(self, location: str) -> LocationProbe:
        if location not in self._locations:
            return LocationProbe(location, LocationEligibility.ELIGIBLE_EMPTY, (), (), "empty")
        return LocationProbe(
            location,
            LocationEligibility.ELIGIBLE_AVAILABLE,
            (_Product(location),),
            (),
            "1 products",
        )

    async def get_product(self, location_id: str, product_id: str) -> Any:
        return _Detail(location_id)


#: Tables the live tests write (truncated between tests; the schema
#: itself comes from real alembic migrations, exactly like production).
_TOUCHED_TABLES = (
    "providers",
    "provider_locations",
    "sellable_offers",
    "provider_routes",
    "catalog_sync_state",
)


def _session_factory_for(url: str) -> Any:
    # NullPool: every checkout opens its own connection, so engines never
    # pin connections to a previous test's event loop.
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def factory_cm() -> Any:
        return factory()

    factory_cm.engine = engine  # type: ignore[attr-defined]
    return factory_cm


@pytest.fixture(scope="module")
def _migrated_db() -> Any:
    """Prepare the scratch database once per module.

    Preferred path is the real alembic migration to head (exactly what
    production and CI run). Minimal embedded servers without the
    ``uuid-ossp`` extension fall back to creating the touched tables from
    the ORM metadata with the ``uuid_generate_v4()`` literal defaults
    swapped for core ``gen_random_uuid()`` — same tables and constraints,
    no migration semantics changed.
    """
    import asyncio
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["DATABASE_URL"] = DB_URL
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        if "uuid-ossp" not in (completed.stderr or ""):
            raise AssertionError(completed.stderr[-2000:])
        asyncio.run(_create_subset_ddl(DB_URL))
    yield None


async def _create_subset_ddl(url: str) -> None:
    """Create the touched tables on servers without ``uuid-ossp``.

    The ORM metadata declares several server defaults as plain strings
    (``uuid_generate_v4()``, ``CURRENT_TIMESTAMP``), which PostgreSQL reads
    as quoted literals; only the alembic migrations carry valid PG
    expressions. The fallback rewrites those two known literal shapes to
    core PG expressions — same columns, constraints and defaults.
    """
    from sqlalchemy import MetaData, text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.schema import DefaultClause

    from cloud_platform.db.base import Base

    subset = MetaData()
    for name in _TOUCHED_TABLES:
        table = Base.metadata.tables[name].to_metadata(subset)
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            literal = str(default.arg)
            # NOTE: assign a DefaultClause, not a bare TextClause — post-copy
            # mutation with a bare TextClause is silently ignored by DDL.
            if "uuid_generate_v4" in literal:
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif literal.strip().upper() == "CURRENT_TIMESTAMP":
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(subset.create_all)
    finally:
        await engine.dispose()


@pytest.fixture()
async def _clean_db(_migrated_db: Any) -> AsyncIterator[Any]:
    """Fresh engine per test (own event loop) over the migrated schema."""
    from sqlalchemy import text

    _ = _migrated_db  # schema is prepared once per module; see below.
    session_factory = _session_factory_for(DB_URL)
    engine = session_factory.engine
    tables = ", ".join(_TOUCHED_TABLES)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
    try:
        yield session_factory
    finally:
        await engine.dispose()


@requires_postgres
class TestLiveAdvisoryLock:
    async def test_key_is_signed_int64(self) -> None:
        assert -(2**63) <= _CATALOG_SYNC_LOCK_KEY <= 2**63 - 1

    async def test_acquire_and_release_without_data_error(self, _clean_db: Any) -> None:
        lock = PostgresAdvisoryCatalogSyncLock(_clean_db)
        async with lock.guard() as acquired:
            assert acquired is True
        # Released: the same key acquires again on a fresh backend.
        async with lock.guard() as reacquired:
            assert reacquired is True

    async def test_second_backend_skips_while_first_holds(self, _clean_db: Any) -> None:
        first = PostgresAdvisoryCatalogSyncLock(_clean_db)
        second = PostgresAdvisoryCatalogSyncLock(_clean_db)
        async with first.guard() as acquired_first:
            assert acquired_first is True
            async with second.guard() as acquired_second:
                assert acquired_second is False
        async with second.guard() as free_again:
            assert free_again is True


@requires_postgres
class TestLiveCoordinatorRepairsStaleLocations:
    async def _run_coordinator(self, session_factory: Any) -> Any:
        from cloud_platform.modules.offers.auto_sync import (
            CatalogAutoSyncCoordinator,
            PricingPolicy,
        )
        from cloud_platform.providers.leaseweb.auto_sync import LeasewebCatalogSyncSource
        from cloud_platform.providers.leaseweb.ordering_sync import (
            LeaseWebOrderingCatalogSyncer,
        )

        halls = tuple(code for code, _city, _country in SIX)
        syncer = LeaseWebOrderingCatalogSyncer(
            session_factory,
            _OrderingProvider(halls),  # type: ignore[arg-type]
        )
        coordinator = CatalogAutoSyncCoordinator(
            sources=[LeasewebCatalogSyncSource(syncer)],
            offers=SqlAlchemySellableOfferRepository(session_factory),
            state=SqlAlchemyCatalogSyncStateRepository(session_factory),
            lock=PostgresAdvisoryCatalogSyncLock(session_factory),
            pricing_policies={
                "leaseweb": PricingPolicy(mode="markup", markup_percent=25, auto_publish=True)
            },
            reference_rates=_DeterministicEurUsdRates(),
        )
        return await coordinator.run()

    async def test_sync_repairs_production_shaped_stale_rows(self, _clean_db: Any) -> None:
        session_factory = _clean_db
        locations = SqlAlchemyLocationRepository(session_factory)
        # Production pin: four halls stuck with raw names, no country/city.
        for code in STALE:
            await locations.upsert(
                LocationRecord(
                    provider_key=PROVIDER,
                    location_id=code,
                    name=code,
                    country_code=None,
                    city=None,
                )
            )
        report = await self._run_coordinator(session_factory)
        # The coordinator completed PAST lock acquisition.
        assert report.ran is True
        rows = {
            record.location_id: record for record in await locations.list_for_provider(PROVIDER)
        }
        for code, city, country in SIX:
            assert rows[code].city == city, code
            assert (rows[code].country_code or "").upper() == country, code
        # Sync-state persistence is visible to the doctor through the same
        # repository it reads: attempted AND success are set.
        states = {
            state.provider_key: state
            for state in await SqlAlchemyCatalogSyncStateRepository(session_factory).list_all()
        }
        assert "leaseweb" in states
        assert states["leaseweb"].last_attempted_at is not None
        assert states["leaseweb"].last_success_at is not None

    async def test_repaired_rows_group_into_two_city_buttons(self, _clean_db: Any) -> None:
        from cloud_platform.modules.checkout.service import OfferCatalogViewService
        from cloud_platform.modules.markets.domain import ProviderCatalog

        session_factory = _clean_db
        locations = SqlAlchemyLocationRepository(session_factory)
        for code in STALE:
            await locations.upsert(
                LocationRecord(
                    provider_key=PROVIDER,
                    location_id=code,
                    name=code,
                    country_code=None,
                    city=None,
                )
            )
        await self._run_coordinator(session_factory)
        service = OfferCatalogViewService(
            offers_repo=SqlAlchemySellableOfferRepository(session_factory),
            provider_registry=_FailingRegistry(),
            wallet_repo=_FailingWallet(),
            signing_key=SIGNING_KEY,
            market_catalog=ProviderCatalog(
                markets={PROVIDER: "foreign"},
                display_names={PROVIDER: "Leaseweb"},
                enabled={},
                families={
                    PROVIDER: {
                        "vps": {
                            "billing_model": BILLING_MODEL_MONTHLY,
                            "display_name": "VPS",
                        },
                        "cloud": {
                            "billing_model": "hourly",
                            "display_name": "Cloud",
                        },
                    }
                },
            ),
            location_repo=locations,
        )
        # Configured two-family selector even though hourly has no inventory.
        families, _, _ = await service.families_screen(PROVIDER)
        assert [(f.family_key, f.available) for f in families] == [
            ("vps", True),
            ("cloud", False),
        ]
        # Six repaired halls group into exactly two city buttons.
        view = await service.cities_screen(PROVIDER, "vps", 1)
        assert [(g.country_code, g.city) for g in view.items] == [
            ("DE", "Frankfurt"),
            ("GB", "London"),
        ]
        frankfurt = next(g for g in view.items if g.city == "Frankfurt")
        assert sorted(frankfurt.location_ids) == ["FRA-01", "FRA-10", "FRA-14"]


class _DeterministicEurUsdRates:
    """Fake EUR/USD reference rates for live pipeline tests (no network).

    The live tests prove sync/repair/pricing mechanics on real PostgreSQL;
    FX correctness itself is covered by the Frankfurter client and pricer
    unit tests. Queued resolutions serve in call order.
    """

    def __init__(self, rate: str = "1.17") -> None:
        from decimal import Decimal

        self._rate = Decimal(rate)

    async def get_rate(self, base: str, quote: str, *, allow_catalog_stale: bool = False) -> Any:
        from datetime import UTC, datetime, timedelta

        from cloud_platform.modules.fx.domain import FxReferenceQuote
        from cloud_platform.modules.fx.service import ReferenceRateResolution

        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=self._rate,
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )

    async def get_catalog_rate(self, base: str, quote: str) -> Any:
        return await self.get_rate(base, quote, allow_catalog_stale=True)

    async def close(self) -> None:
        return None


def _official_types_payload() -> dict[str, Any]:
    """Official ``/publicCloud/v1/instanceTypes`` shape (verbatim layout)."""
    return {
        "instanceTypes": [
            {
                "name": "lsw.c3.large",
                "resources": {
                    "cpu": {"value": 2, "unit": "vCPU"},
                    "memory": {"value": 3, "unit": "GiB"},
                    "publicNetworkSpeed": {"value": 1, "unit": "Gbps"},
                    "privateNetworkSpeed": {"value": 100, "unit": "Mbps"},
                },
                "prices": {"hourly": "0.0395", "monthly": "26.0200"},
                "storageTypes": ["CENTRAL"],
                "minDiskSize": 5,
            }
        ],
        "_metadata": {"currency": "EUR", "currencySymbol": "€"},
    }


class _StubCloudTransport:
    """Read-only transport double serving official-shaped payloads."""

    async def request(
        self, method: str, path: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        assert method == "GET", "live sync acceptance performs reads only"
        if path == "/publicCloud/v1/regions":
            return {
                "regions": [
                    {
                        "name": "eu-west-3",
                        "country": "DE",
                        "displayName": "Frankfurt",
                        "city": "Frankfurt",
                    }
                ]
            }
        if path == "/publicCloud/v1/instanceTypes":
            return _official_types_payload()
        raise AssertionError(f"unexpected provider read: {path}")

    async def aclose(self) -> None:
        return None


@requires_postgres
class TestLiveHourlySyncAcceptance:
    """Official hourly payload -> sellable Cloud offer on real PostgreSQL."""

    async def test_hourly_offer_becomes_sellable(self, _clean_db: Any) -> None:
        from cloud_platform.modules.checkout.service import OfferCatalogViewService
        from cloud_platform.modules.markets.domain import ProviderCatalog
        from cloud_platform.modules.offers.auto_sync import (
            CatalogAutoSyncCoordinator,
            PricingPolicy,
        )
        from cloud_platform.providers.leaseweb.cloud import LeasewebHourlyCloudProvider
        from cloud_platform.providers.leaseweb.cloud_auto_sync import (
            LeasewebHourlyCloudSyncSource,
        )
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        session_factory = _clean_db
        provider = LeasewebHourlyCloudProvider.__new__(LeasewebHourlyCloudProvider)
        provider._transport = _StubCloudTransport()
        try:
            syncer = LeasewebHourlyCloudSyncer(session_factory, accounts={"north": provider})
            coordinator = CatalogAutoSyncCoordinator(
                sources=[LeasewebHourlyCloudSyncSource(syncer)],
                offers=SqlAlchemySellableOfferRepository(session_factory),
                state=SqlAlchemyCatalogSyncStateRepository(session_factory),
                lock=PostgresAdvisoryCatalogSyncLock(session_factory),
                pricing_policies={
                    "leaseweb.hourly": PricingPolicy(
                        mode="markup", markup_percent=25, auto_publish=True
                    )
                },
                reference_rates=_DeterministicEurUsdRates(),
            )
            report = await coordinator.run()
            assert report.ran is True
            offers_repo = SqlAlchemySellableOfferRepository(session_factory)
            stored = await offers_repo.get_by_ref(
                "leaseweb", "lsw.c3.large", "eu-west-3", provider_account_id="north"
            )
            assert stored is not None
            assert stored.provider_account_id == "north"
            assert stored.provider_cost_minor == 4  # 0.0395 EUR, HALF_UP
            assert stored.provider_cost_currency == "EUR"
            assert stored.provider_available is True
            # Exact native rate through FX first: 0.0395 * 1.17 * 1.25
            # = 0.05776875 USD -> 6 ceiling USD cents (never via rounded 4c).
            assert stored.selling_price_minor == 6
            assert stored.selling_currency == "USD"
            assert stored.enabled is True
            assert stored.sellable is True
            states = {
                state.provider_key: state
                for state in await SqlAlchemyCatalogSyncStateRepository(session_factory).list_all()
            }
            assert "leaseweb.hourly" in states
            assert states["leaseweb.hourly"].last_success_at is not None
            service = OfferCatalogViewService(
                offers_repo=offers_repo,
                provider_registry=_FailingRegistry(),
                wallet_repo=_FailingWallet(),
                signing_key=SIGNING_KEY,
                market_catalog=ProviderCatalog(
                    markets={PROVIDER: "foreign"},
                    display_names={PROVIDER: "Leaseweb"},
                    enabled={},
                    families={
                        PROVIDER: {
                            "vps": {
                                "billing_model": BILLING_MODEL_MONTHLY,
                                "display_name": "VPS",
                            },
                            "cloud": {
                                "billing_model": "hourly",
                                "display_name": "Cloud",
                            },
                        }
                    },
                ),
                location_repo=SqlAlchemyLocationRepository(session_factory),
            )
            families, _, _ = await service.families_screen(PROVIDER)
            assert [(f.family_key, f.available) for f in families] == [
                ("vps", False),
                ("cloud", True),
            ]
            cities = await service.cities_screen(PROVIDER, "cloud", 1)
            assert [(g.country_code, g.city) for g in cities.items] == [("DE", "Frankfurt")]
        finally:
            await provider.close()


@requires_postgres
class TestLiveStorefrontFxBoundary:
    """Real PostgreSQL read path across the reference-rate validity boundary.

    Pins the 2026-09-26 blackout with the REAL resolver, pricer, repository and
    browse chain: a reference quote whose own TTL is about to run out must not
    be published as a short-lived "fresh" price, and the foreign market must
    stay visible on real rows while a bounded last-known-good rate is legal.
    """

    #: 900s cadence + 600s job budget + 300s margin (the incident defaults).
    HORIZON_SECONDS = 900 + 600 + 300

    async def test_foreign_market_survives_the_reference_quote_expiry(self, _clean_db: Any) -> None:
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from cloud_platform.modules.checkout.service import OfferCatalogViewService
        from cloud_platform.modules.fx.cache import build_fx_cache
        from cloud_platform.modules.fx.domain import FxReferenceQuote, FxUnavailableError
        from cloud_platform.modules.fx.ports import reference_rate_cache_key
        from cloud_platform.modules.fx.service import (
            GlobalFiatFxConfig,
            GlobalFiatFxResolver,
        )
        from cloud_platform.modules.markets.domain import ProviderCatalog
        from cloud_platform.modules.offers.auto_sync import (
            CatalogAutoSyncCoordinator,
            PricingPolicy,
        )
        from cloud_platform.providers.leaseweb.cloud import LeasewebHourlyCloudProvider
        from cloud_platform.providers.leaseweb.cloud_auto_sync import (
            LeasewebHourlyCloudSyncSource,
        )
        from cloud_platform.providers.leaseweb.cloud_sync import LeasewebHourlyCloudSyncer

        session_factory = _clean_db
        stale_limit = 86400
        now = datetime.now(UTC)
        observed = now - timedelta(seconds=3600)

        class _FrankfurterOutage:
            """Read-only provider double: every refresh attempt fails."""

            source_name = "frankfurter"

            async def get_reference_rate(self, base: str, quote: str) -> Any:
                raise FxUnavailableError("frankfurter unavailable")

            async def close(self) -> None:
                return None

        cache = build_fx_cache(backend="memory")
        await cache.put(
            reference_rate_cache_key("EUR", "USD", source="frankfurter"),
            FxReferenceQuote(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.17"),
                source="frankfurter",
                source_market="EUR/USD",
                provider_date=observed.date(),
                observed_at=observed,
                # The incident shape: technically still fresh, seconds of TTL.
                expires_at=now + timedelta(seconds=5),
            ),
        )
        resolver = GlobalFiatFxResolver(
            source=_FrankfurterOutage(),  # type: ignore[arg-type]
            cache=cache,
            config=GlobalFiatFxConfig(
                quote_ttl_seconds=3600,
                max_stale_seconds=345600,
                catalog_max_stale_seconds=stale_limit,
            ),
        )
        provider = LeasewebHourlyCloudProvider.__new__(LeasewebHourlyCloudProvider)
        provider._transport = _StubCloudTransport()
        offers_repo = SqlAlchemySellableOfferRepository(
            session_factory,
            catalog_currency="USD",
            catalog_stale_limit_seconds=stale_limit,
        )
        try:
            coordinator = CatalogAutoSyncCoordinator(
                sources=[
                    LeasewebHourlyCloudSyncSource(
                        LeasewebHourlyCloudSyncer(session_factory, accounts={"north": provider})
                    )
                ],
                offers=offers_repo,
                state=SqlAlchemyCatalogSyncStateRepository(session_factory),
                lock=PostgresAdvisoryCatalogSyncLock(session_factory),
                pricing_policies={
                    "leaseweb.hourly": PricingPolicy(
                        mode="markup", markup_percent=25, auto_publish=True
                    )
                },
                reference_rates=resolver,
                catalog_currency="USD",
                catalog_min_fresh_lifetime_seconds=self.HORIZON_SECONDS,
            )
            report = await coordinator.run()
            assert report.ran is True
            provider_report = report.providers[0]
            assert provider_report.previous_sellable == 0
            assert provider_report.resulting_sellable == 1
            # The bounded last-known-good rate kept the market visible, and the
            # run says so explicitly instead of leaving an unexplained
            # fx_stale=true behind.
            assert any(
                warning.startswith("storefront blackout prevented")
                and "reason=bounded_catalog_fx" in warning
                and "pair=EUR/USD" in warning
                for warning in provider_report.warnings
            )
            assert not any(
                warning.startswith("storefront blackout occurred")
                for warning in provider_report.warnings
            )
            observations = {entry.pair: entry for entry in provider_report.fx_observations}
            assert observations["EUR/USD"].stale is True
            assert (
                observations["EUR/USD"].remaining_lifetime_at_sync_start_seconds
                >= self.HORIZON_SECONDS
            )

            sellable = await offers_repo.list_sellable()
            assert [row.product_id for row in sellable] == ["lsw.c3.large"]
            stored = sellable[0]
            assert stored.pricing_metadata["fx_stale"] is True
            valid_until = datetime.fromisoformat(
                str(stored.pricing_metadata["catalog_valid_until"])
            )
            # Bounded by the audited stale window, not by the 5-second TTL the
            # quote itself had left: the price outlives the next refresh.
            assert valid_until == observed + timedelta(seconds=stale_limit)
            assert valid_until - datetime.now(UTC) >= timedelta(seconds=self.HORIZON_SECONDS)

            service = OfferCatalogViewService(
                offers_repo=offers_repo,
                provider_registry=_FailingRegistry(),
                wallet_repo=_FailingWallet(),
                signing_key=SIGNING_KEY,
                market_catalog=ProviderCatalog(
                    markets={PROVIDER: "foreign"},
                    display_names={PROVIDER: "Leaseweb"},
                    enabled={},
                    families={
                        PROVIDER: {
                            "vps": {
                                "billing_model": BILLING_MODEL_MONTHLY,
                                "display_name": "VPS",
                            },
                            "cloud": {
                                "billing_model": "hourly",
                                "display_name": "Cloud",
                            },
                        }
                    },
                ),
                location_repo=SqlAlchemyLocationRepository(session_factory),
            )
            views, _back = await service.providers_screen("foreign")
            assert [(view.provider_key, view.offer_count) for view in views] == [(PROVIDER, 1)]
        finally:
            await provider.close()


class _FailingRegistry:
    def get(self, key: str) -> Any:
        raise KeyError(key)


class _FailingWallet:
    async def get(self, user_id: Any) -> Any:
        raise AssertionError("no wallet reads in grouping tests")
