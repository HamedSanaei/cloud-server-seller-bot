"""Real transaction/ownership proofs for bounded catalog batches."""

from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from cloud_platform.db.base import SellableOffer as Row
from cloud_platform.modules.offers.batch import ObservationBuffer
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.observability.catalog import install_sql_instrumentation, stage
from tests.live.catalog_performance_support import observation, scratch_catalog

pytestmark = pytest.mark.skipif(
    not os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL"),
    reason="explicit scratch PostgreSQL required",
)


async def test_batch_preserves_manual_fields_and_avoids_unchanged_updates():
    async with scratch_catalog() as (engine, factory):
        install_sql_instrumentation(engine)
        repo = SqlAlchemySellableOfferRepository(factory)
        items = [observation(i) for i in range(100)]
        with stage("benchmark") as cold:
            rows = await repo.upsert_provider_batch(items)
        assert cold.queries == 2 and cold.commits == 1
        async with factory.begin() as session:
            row = await session.get(Row, rows[0].id)
            row.operator_disabled = True
            row.auto_priced = False
            row.selling_price_minor = 777
        with stage("benchmark") as sample:
            await repo.upsert_provider_batch(items)
        assert sample.queries == 1 and sample.commits == 1
        async with factory() as session:
            row = await session.get(Row, rows[0].id)
            assert row.operator_disabled and not row.auto_priced and row.selling_price_minor == 777
        changed = dict(items[0], update=replace(items[0]["update"], provider_cost_minor=123))
        await repo.upsert_provider_batch([changed])
        result = await repo.get_by_ref(
            "leaseweb",
            changed["product_id"],
            "AMS-01",
            provider_account_id="account-a",
            billing_model="hourly",
        )
        assert result.provider_cost_minor == 123 and result.selling_price_minor == 777


async def test_invalid_batch_rolls_back_and_buffer_recovers_valid_rows_only():
    async with scratch_catalog() as (_, factory):
        repo = SqlAlchemySellableOfferRepository(factory)
        good = observation(1)
        invalid = dict(observation(2), provider_key="x" * 300)
        with pytest.raises(DBAPIError):
            await repo.upsert_provider_batch([good, invalid])
        assert await repo.list_all() == []
        buffer = ObservationBuffer(repo)
        await buffer.upsert_from_provider(**good)
        await buffer.upsert_from_provider(**invalid)
        success, failures = await buffer.flush()
        assert success == 1 and len(failures) == 1
        assert len(await repo.list_all()) == 1


async def test_owner_adoption_and_account_scoped_batch_reads_match_single_path():
    async with scratch_catalog() as (_, factory):
        repo = SqlAlchemySellableOfferRepository(factory)
        original = (await repo.upsert_provider_batch([observation(1)]))[0]
        moved = (await repo.upsert_provider_batch([observation(1, account="account-b")]))[0]
        assert moved.id == original.id and moved.provider_account_id == "account-b"
        assert (
            await repo.get_many_by_refs(
                "leaseweb", "hourly", [("account-a", moved.product_id, moved.location_id)]
            )
            == []
        )
        assert (
            len(
                await repo.get_many_by_refs(
                    "leaseweb", "hourly", [("account-b", moved.product_id, moved.location_id)]
                )
            )
            == 1
        )


async def test_catalog_advisory_lock_still_serializes_backends():
    from cloud_platform.modules.catalog.repository import PostgresAdvisoryCatalogSyncLock

    lock_key = int.from_bytes(uuid4().bytes[:8], "big", signed=True)
    async with scratch_catalog() as (_, factory):
        # Separate engine avoids a single-pool-slot deadlock in this proof.
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine2 = create_async_engine(
            os.environ["CLOUD_PLATFORM_TEST_POSTGRES_URL"], hide_parameters=True
        )
        try:
            async with PostgresAdvisoryCatalogSyncLock(factory, key=lock_key).guard() as first:
                async with PostgresAdvisoryCatalogSyncLock(
                    async_sessionmaker(engine2), key=lock_key
                ).guard() as second:
                    assert first and not second
        finally:
            await engine2.dispose()


async def test_batch_pricing_matches_single_path_and_cas_preserves_manual_changes():
    from cloud_platform.modules.offers.domain import PricingPolicy
    from cloud_platform.modules.offers.pricing import CatalogOfferPricer

    async with scratch_catalog() as (engine, factory):
        install_sql_instrumentation(engine)
        repo = SqlAlchemySellableOfferRepository(factory)
        offers = await repo.upsert_provider_batch([observation(i) for i in range(3)])
        requests = []
        for offer in offers:
            priced = await CatalogOfferPricer(None, "USD").price_auto(
                offer, PricingPolicy(markup_percent=10)
            )
            requests.append(
                dict(
                    offer_id=offer.id,
                    expected_cost_minor=offer.provider_cost_minor,
                    expected_cost_currency="USD",
                    expected_provider_rate=offer.billing_parameters["provider_hourly_rate"],
                    selling_price_minor=priced.selling_price_minor,
                    selling_currency="USD",
                    pricing_metadata=dict(priced.pricing_metadata),
                    expected_observation=offer,
                )
            )
        # An operator commits after the pricing read, before catalog locks.
        async with factory.begin() as session:
            manual = await session.get(Row, offers[0].id)
            manual.auto_priced = False
            manual.selling_price_minor = 777
            moved = await session.get(Row, offers[1].id)
            moved.provider_account_id = "account-b"
        results = await repo.set_auto_prices_batch_if_current(requests)
        assert results[0] is None and results[1] is None and results[2] is not None
        async with factory() as session:
            assert (await session.get(Row, offers[0].id)).selling_price_minor == 777
            assert (await session.get(Row, offers[1].id)).selling_price_minor == 0
        # Identical evidence is a no-op; no refresh timestamp is invented.
        request = dict(requests[2], expected_observation=results[2])
        with stage("benchmark") as sample:
            again = await repo.set_auto_prices_batch_if_current([request])
        assert sample.queries == 1 and again[0] == results[2]
        single = await repo.set_auto_price_if_current(**request)
        assert single == results[2]


async def test_batch_pricing_rolls_back_all_rows_on_invalid_provenance():
    from cloud_platform.modules.offers.domain import PricingPolicy
    from cloud_platform.modules.offers.pricing import CatalogOfferPricer

    async with scratch_catalog() as (_, factory):
        repo = SqlAlchemySellableOfferRepository(factory)
        offers = await repo.upsert_provider_batch([observation(1), observation(2)])
        requests = []
        for offer in offers:
            priced = await CatalogOfferPricer(None, "USD").price_auto(
                offer, PricingPolicy(markup_percent=10)
            )
            requests.append(
                dict(
                    offer_id=offer.id,
                    expected_cost_minor=offer.provider_cost_minor,
                    expected_cost_currency="USD",
                    expected_provider_rate="1",
                    selling_price_minor=priced.selling_price_minor,
                    selling_currency="USD",
                    pricing_metadata=dict(priced.pricing_metadata),
                )
            )
        requests[1]["selling_price_minor"] = -1
        with pytest.raises(ValueError):
            await repo.set_auto_prices_batch_if_current(requests)
        assert all(offer.selling_price_minor == 0 for offer in await repo.list_all())


async def test_concurrent_manual_price_wins_after_row_lock_is_released():
    import asyncio

    from sqlalchemy import select

    async with scratch_catalog(pool_size=2) as (_, factory):
        repo = SqlAlchemySellableOfferRepository(factory)
        offer = (await repo.upsert_provider_batch([observation(1)]))[0]
        async with factory.begin() as operator:
            row = (
                await operator.execute(select(Row).where(Row.id == offer.id).with_for_update())
            ).scalar_one()
            task = asyncio.create_task(
                repo.set_auto_prices_batch_if_current(
                    [
                        dict(
                            offer_id=offer.id,
                            expected_cost_minor=100,
                            expected_cost_currency="USD",
                            expected_provider_rate="1",
                            selling_price_minor=110,
                            selling_currency="USD",
                            pricing_metadata={},
                            expected_observation=offer,
                        )
                    ]
                )
            )
            await asyncio.sleep(0.02)
            assert not task.done()
            row.auto_priced = False
            row.selling_price_minor = 888
        assert await asyncio.wait_for(task, 3) == [None]
        async with factory() as session:
            assert (await session.get(Row, offer.id)).selling_price_minor == 888


async def test_concurrent_coordinators_skip_overlap_through_real_advisory_lock():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from cloud_platform.modules.catalog.repository import PostgresAdvisoryCatalogSyncLock
    from cloud_platform.modules.offers.auto_sync import CatalogAutoSyncCoordinator
    from cloud_platform.modules.offers.domain import CatalogSyncReport

    entered, release = asyncio.Event(), asyncio.Event()

    async def discover():
        entered.set()
        await release.wait()
        return CatalogSyncReport(provider_key="leaseweb", ok=False, errors=("synthetic denial",))

    async with scratch_catalog(pool_size=2) as (_, factory):
        coordinator = CatalogAutoSyncCoordinator(
            sources=[SimpleNamespace(provider_key="leaseweb", sync_catalog=discover)],
            offers=SqlAlchemySellableOfferRepository(factory),
            state=AsyncMock(),
            lock=PostgresAdvisoryCatalogSyncLock(
                factory, key=int.from_bytes(uuid4().bytes[:8], "big", signed=True)
            ),
        )
        first = asyncio.create_task(coordinator.run())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert not (await asyncio.wait_for(coordinator.run(), 3)).ran
        finally:
            release.set()
        assert (await asyncio.wait_for(first, 3)).ran


async def test_batch_routes_keep_inventory_and_reject_older_evidence():
    from datetime import UTC, datetime, timedelta

    from cloud_platform.modules.provider_routes.domain import RouteObservation, RouteState
    from cloud_platform.modules.provider_routes.repository import SqlAlchemyProviderRouteRepository
    from cloud_platform.providers.routing import CredentialAccountState

    async with scratch_catalog() as (engine, factory):
        install_sql_instrumentation(engine)
        repo = SqlAlchemyProviderRouteRepository(factory)
        now = datetime.now(UTC)
        observations = [
            RouteObservation(
                "account-a",
                f"region-{i}",
                RouteState.ELIGIBLE_AVAILABLE,
                product_ids=("plan",),
                succeeded=True,
            )
            for i in range(100)
        ]
        args = dict(
            provider_key="leaseweb",
            priority_of=lambda _: 10,
            account_state_of=lambda _: CredentialAccountState.ACTIVE,
        )
        with stage("benchmark") as sample:
            await repo.upsert_observations_batch(**args, observations=observations, at=now)
        assert sample.queries == 1 and sample.commits == 1
        failed = [
            replace(o, state=RouteState.TRANSIENT_UNKNOWN, product_ids=(), succeeded=False)
            for o in observations
        ]
        await repo.upsert_observations_batch(
            **args, observations=failed, at=now + timedelta(seconds=1)
        )
        rows = await repo.list_for_provider("leaseweb")
        assert len(rows) == 100 and all(r.product_ids == ("plan",) for r in rows)
        assert all(
            r.last_success_at == now and r.state is RouteState.TRANSIENT_UNKNOWN for r in rows
        )
        await repo.upsert_observations_batch(**args, observations=observations, at=now)
        assert all(
            r.state is RouteState.TRANSIENT_UNKNOWN
            for r in await repo.list_for_provider("leaseweb")
        )


async def test_location_batch_noop_preserves_legacy_rows_and_metadata_timestamps():
    from sqlalchemy import select

    from cloud_platform.db.base import ProviderLocation
    from cloud_platform.modules.catalog.domain import LocationRecord
    from cloud_platform.modules.catalog.repository import SqlAlchemyLocationRepository

    async with scratch_catalog(extra_tables=(ProviderLocation.__table__,)) as (engine, factory):
        install_sql_instrumentation(engine)
        repo = SqlAlchemyLocationRepository(factory)
        legacy = LocationRecord("leaseweb", "EU-WEST-3", "historical display row")
        await repo.upsert(legacy)
        locations = [
            LocationRecord("leaseweb", f"PAR-{i:02}", "Paris", country_code="FR", city="Paris")
            for i in range(15)
        ]
        await repo.upsert_batch(locations)
        async with factory() as session:
            before = (await session.execute(select(ProviderLocation.__table__))).mappings().all()
        with stage("location_noop") as sample:
            await repo.upsert_batch(locations)
        assert sample.queries == 2 and sample.commits == 0
        async with factory() as session:
            after = (await session.execute(select(ProviderLocation.__table__))).mappings().all()
        assert sorted(before, key=lambda r: r["location_id"]) == sorted(
            after, key=lambda r: r["location_id"]
        )
        await repo.upsert_batch([replace(locations[0], name="Paris updated")])
        rows = {r.location_id: r for r in await repo.list_for_provider("leaseweb")}
        assert rows["PAR-00"].name == "Paris updated"
        assert rows["EU-WEST-3"] == legacy


async def test_narrow_catalog_reads_keep_family_ownership_and_full_sellability_gate(monkeypatch):
    from sqlalchemy import update

    from cloud_platform.core.container import Container
    from cloud_platform.modules.offers.domain import (
        BILLING_MODEL_MONTHLY,
        is_sellable_in_currency,
    )

    async with scratch_catalog() as (_, factory):
        repo = SqlAlchemySellableOfferRepository(factory)
        items = [observation(i) for i in range(8)]
        items[1]["provider_key"] = "hetzner"  # count is not Leaseweb-only
        items[7]["provider_account_id"] = None  # legacy unscoped ownership
        items[7]["update"] = replace(
            items[7]["update"],
            billing_model=BILLING_MODEL_MONTHLY,
            billing_parameters={"provider_monthly_rate": "1"},
        )
        offers = await repo.upsert_provider_batch(items)
        async with factory.begin() as session:
            await session.execute(
                update(Row).values(
                    enabled=True, auto_priced=False, selling_price_minor=150, selling_currency="USD"
                )
            )
            await session.execute(update(Row).where(Row.id == offers[2].id).values(enabled=False))
            await session.execute(
                update(Row).where(Row.id == offers[3].id).values(operator_disabled=True)
            )
            await session.execute(
                update(Row).where(Row.id == offers[4].id).values(provider_available=False)
            )
            await session.execute(
                update(Row).where(Row.id == offers[5].id).values(selling_price_minor=0)
            )
            await session.execute(
                update(Row)
                .where(Row.id == offers[6].id)
                .values(
                    auto_priced=True,
                    pricing_metadata={},  # published but unproven
                )
            )
        assert await repo.available_refs("leaseweb", billing_model=BILLING_MODEL_MONTHLY) == {
            ("", items[7]["product_id"], "AMS-01")
        }
        expected = {
            row.id
            for row in await repo.list_all()
            if row.billing_model == "hourly" and is_sellable_in_currency(row, "USD")
        }
        assert expected == {offers[0].id, offers[1].id}
        narrowed = await repo.published_candidates(billing_model="hourly")
        assert {row.id for row in narrowed if is_sellable_in_currency(row, "USD")} == expected
        monkeypatch.setattr(Container, "sellable_offer_repository", lambda _: repo)
        assert await Container.sellable_cloud_offer_count(object.__new__(Container)) == 2


async def test_legacy_namespace_sync_preserves_orders_resources_and_manual_prices():
    from sqlalchemy import insert, select, update

    from cloud_platform.db.base import (
        ProviderAccount,
        ProviderLocation,
        ProviderOrder,
        ProviderRoute,
        Server,
        User,
    )
    from cloud_platform.modules.catalog.domain import LocationRecord
    from cloud_platform.modules.catalog.repository import (
        SqlAlchemyLocationRepository,
        provider_key_to_uuid,
    )
    from cloud_platform.modules.offers.domain import BILLING_MODEL_MONTHLY
    from cloud_platform.providers.leaseweb.errors import LeasewebValidationError
    from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer
    from tests.live.test_postgres_catalog_sync import _OrderingProvider

    class Provider(_OrderingProvider):
        async def get_product(self, location_id, product_id):
            raise LeasewebValidationError("product not available in this location")

    extras = (ProviderLocation.__table__, ProviderOrder.__table__)
    async with scratch_catalog(extra_tables=extras) as (_, factory):
        locations = SqlAlchemyLocationRepository(factory)
        for code in ("eu-west-3", "EU-WEST-3"):
            await locations.upsert(LocationRecord("leaseweb", code, "legacy cloud row"))
        repo = SqlAlchemySellableOfferRepository(factory)
        monthly = dict(observation(1, account="north"), product_id="VPS02_1", location_id="FRA-10")
        monthly["update"] = replace(
            monthly["update"],
            billing_model=BILLING_MODEL_MONTHLY,
            billing_parameters={"provider_monthly_rate": "1"},
        )
        cloud = dict(observation(2), location_id="eu-west-3")
        offer, _ = await repo.upsert_provider_batch([monthly, cloud])
        user_id, account_id, server_id, order_id, route_id = (uuid4() for _ in range(5))
        provider_id = provider_key_to_uuid("leaseweb")
        async with factory.begin() as session:
            await session.execute(
                update(Row)
                .where(Row.id == offer.id)
                .values(
                    selling_price_minor=777,
                    selling_currency="USD",
                    auto_priced=False,
                    enabled=False,
                    operator_disabled=True,
                )
            )
            await session.execute(
                insert(User).values(
                    id=user_id, username="catalog-fixture", email="catalog@example.test"
                )
            )
            await session.execute(
                insert(ProviderAccount).values(
                    id=account_id, user_id=user_id, provider_id=provider_id
                )
            )
            await session.execute(
                insert(Server).values(
                    id=server_id,
                    user_id=user_id,
                    provider_id=provider_id,
                    provider_account_id=account_id,
                    credential_account_id="north",
                    state="running",
                    provider_server_id="existing-customer-vps",
                    price_per_quantum=777,
                    currency="USD",
                    billing_model=BILLING_MODEL_MONTHLY,
                    idempotency_key="existing-server-command",
                )
            )
            await session.execute(
                insert(ProviderOrder).values(
                    id=order_id,
                    server_id=server_id,
                    offer_id=offer.id,
                    provider_key="leaseweb",
                    credential_account_id="north",
                    operation_key="existing-order-command",
                    provider_order_id="existing-provider-order",
                    status="active",
                    provider_cost_minor=100,
                    provider_cost_currency="USD",
                    selling_price_minor=777,
                    selling_currency="USD",
                )
            )
            await session.execute(
                insert(ProviderRoute).values(
                    id=route_id,
                    provider_key="leaseweb",
                    credential_account_id="north",
                    location_id="EU-WEST-3",
                    state="transient_unknown",
                    account_state="active",
                    priority=100,
                    product_ids=[],
                )
            )

        async def protected_snapshot():
            async with factory() as session:
                return {
                    model.__tablename__: (await session.execute(select(model.__table__)))
                    .mappings()
                    .all()
                    for model in (Server, ProviderOrder)
                }

        before = await protected_snapshot()
        async with factory() as session:
            cloud_before = (
                (await session.execute(select(Row.__table__).where(Row.location_id == "eu-west-3")))
                .mappings()
                .one()
            )
            route_before = (
                (
                    await session.execute(
                        select(ProviderRoute.__table__).where(ProviderRoute.id == route_id)
                    )
                )
                .mappings()
                .one()
            )
        syncer = LeaseWebOrderingCatalogSyncer(
            factory, accounts={"north": Provider(("FRA-10",)), "uk": Provider(("LON-11",))}
        )
        await syncer.sync_products()
        first = await repo.get(offer.id)
        await syncer.sync_products()
        current = await repo.get(offer.id)
        assert current == first
        assert current.provider_cost_minor == 624
        assert current.provider_cost_currency == "EUR"
        assert current.billing_parameters["provider_monthly_rate"] == "6.24"
        assert current.provider_account_id == "north" and current.provider_available
        assert current.selling_price_minor == 777 and current.operator_disabled
        assert not current.enabled and not current.auto_priced
        assert await protected_snapshot() == before
        async with factory() as session:
            assert (
                await session.execute(select(Row.__table__).where(Row.location_id == "eu-west-3"))
            ).mappings().one() == cloud_before
            assert (
                await session.execute(
                    select(ProviderRoute.__table__).where(ProviderRoute.id == route_id)
                )
            ).mappings().one() == route_before
        records = {r.location_id: r for r in await locations.list_for_provider("leaseweb")}
        assert records["eu-west-3"].name == records["EU-WEST-3"].name == "legacy cloud row"
