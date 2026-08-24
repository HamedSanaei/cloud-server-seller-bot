"""Tests for the safe customer catalog cache (M04-008).

Acceptance: the cache invalidates on offer/price change.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.catalog.cache import CatalogCache
from cloud_platform.modules.catalog.domain import (
    CatalogEntrySpec,
    CatalogRepository,
    OfferRef,
    OfferState,
)

REF_A = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1")
REF_B = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="nbg1")
ROW_ID = uuid4()
OTHER_ID = uuid4()


def _offer(ref: OfferRef, price: int = 1000) -> OfferState:
    return OfferState(
        id=ROW_ID,
        ref=ref,
        name=f"plan {ref.location_id}",
        enabled=True,
        price_per_quantum=price,
        currency="EUR",
    )


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeInner:
    """CatalogRepository fake with a call counter and a mutable offer table."""

    def __init__(self, offers: dict[str, OfferState | None] | None = None) -> None:
        self.offers = dict(offers or {})
        self.get_calls: list[OfferRef] = []
        self.upsert_calls: list[CatalogEntrySpec] = []
        self.enable_calls: list[tuple[OfferRef, bool]] = []

    async def get_offer(self, ref: OfferRef) -> OfferState | None:
        self.get_calls.append(ref)
        return self.offers.get(ref.key)

    async def upsert_entry(self, spec: CatalogEntrySpec) -> bool:
        self.upsert_calls.append(spec)
        ref = OfferRef(
            provider_key=spec.provider_key,
            plan_id=spec.plan_id,
            location_id=spec.location_id,
        )
        created = ref.key not in self.offers
        if created:
            self.offers[ref.key] = OfferState(
                id=uuid4(),
                ref=ref,
                name=spec.name,
                enabled=True,
                price_per_quantum=spec.price_per_quantum,
                currency=spec.currency,
            )
        return created

    async def set_offer_enabled(self, ref: OfferRef, enabled: bool) -> None:
        self.enable_calls.append((ref, enabled))
        current = self.offers.get(ref.key)
        if current is None:
            raise LookupError(f"offer {ref.key} not found")
        self.offers[ref.key] = OfferState(
            id=current.id,
            ref=ref,
            name=current.name,
            enabled=enabled,
            price_per_quantum=current.price_per_quantum,
            currency=current.currency,
        )


def _cache(inner: FakeInner, ttl: float = 60.0, clock: FakeClock | None = None) -> CatalogCache:
    return CatalogCache(inner, ttl_seconds=ttl, clock=clock or FakeClock())


def _spec(location: str = "fsn1", price: int = 2000) -> CatalogEntrySpec:
    return CatalogEntrySpec(
        provider_key="hetzner",
        plan_id="cx22",
        location_id=location,
        name="CAX22",
        architecture="x86_64",
        vcpu=2,
        memory_mb=4096,
        disk_gb=40,
        currency="EUR",
        price_per_quantum=price,
    )


class TestReads:
    async def test_second_read_is_served_from_cache(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        cache = _cache(inner)

        first = await cache.get_offer(REF_A)
        second = await cache.get_offer(REF_A)

        assert first == second
        assert inner.get_calls == [REF_A]  # inner hit exactly once
        stats = cache.stats
        assert stats.hits == 1 and stats.misses == 1

    async def test_distinct_refs_cached_independently(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A), REF_B.key: _offer(REF_B)})
        cache = _cache(inner)

        await cache.get_offer(REF_A)
        await cache.get_offer(REF_B)
        await cache.get_offer(REF_A)

        assert inner.get_calls == [REF_A, REF_B]
        assert cache.stats.entries == 2

    async def test_unknown_offer_negative_cached(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        cache = _cache(inner)

        assert await cache.get_offer(REF_B) is None
        assert await cache.get_offer(REF_B) is None

        assert inner.get_calls == [REF_B]  # miss cached, no repeated store hit

    async def test_ttl_zero_disables_caching(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        cache = _cache(inner, ttl=0)

        await cache.get_offer(REF_A)
        await cache.get_offer(REF_A)

        assert inner.get_calls == [REF_A, REF_A]
        assert cache.stats.entries == 0

    async def test_negative_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="ttl_seconds"):
            _cache(FakeInner(), ttl=-1)


class TestInvalidationOnChanges:
    async def test_price_upsert_invalidates(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A, price=1000)})
        cache = _cache(inner)

        before = await cache.get_offer(REF_A)
        assert before is not None and before.price_per_quantum == 1000

        # price change flows through the cache (ingestion path)
        await cache.upsert_entry(_spec(price=1500))
        inner.offers[REF_A.key] = _offer(REF_A, price=1500)

        after = await cache.get_offer(REF_A)
        assert after is not None and after.price_per_quantum == 1500
        assert len(inner.get_calls) == 2  # re-fetched after the change

    async def test_visibility_change_invalidates(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        cache = _cache(inner)

        visible = await cache.get_offer(REF_A)
        assert visible is not None and visible.enabled

        await cache.set_offer_enabled(REF_A, False)

        hidden = await cache.get_offer(REF_A)
        assert hidden is not None and not hidden.enabled
        assert len(inner.get_calls) == 2

    async def test_visibility_lookup_error_still_propagates(self) -> None:
        inner = FakeInner({})
        cache = _cache(inner)

        with pytest.raises(LookupError):
            await cache.set_offer_enabled(REF_A, True)

    async def test_upsert_return_value_passthrough(self) -> None:
        inner = FakeInner({})
        cache = _cache(inner)

        assert await cache.upsert_entry(_spec()) is True  # created
        assert await cache.upsert_entry(_spec()) is False  # updated

    async def test_invalidation_during_fetch_not_cached(self) -> None:
        """An admin write racing a fetch must not be shadowed by it."""
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        cache: CatalogCache | None = None

        async def racing_get(ref: OfferRef) -> OfferState | None:
            # a concurrent writer invalidates while this fetch is in flight
            assert cache is not None
            cache.invalidate()
            inner.get_calls.append(ref)
            return inner.offers.get(ref.key)

        inner.get_offer = racing_get  # type: ignore[method-assign]
        cache = CatalogCache(inner, ttl_seconds=60, clock=FakeClock())

        value = await cache.get_offer(REF_A)
        assert value is not None

        assert cache.stats.entries == 0  # the racing fetch result was not cached
        await cache.get_offer(REF_A)
        assert len(inner.get_calls) == 2  # next read fetched again


class TestTtlAndManualInvalidation:
    async def test_entry_expires_after_ttl(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        clock = FakeClock()
        cache = _cache(inner, ttl=30.0, clock=clock)

        await cache.get_offer(REF_A)
        clock.advance(29.9)
        await cache.get_offer(REF_A)  # still fresh
        clock.advance(0.2)  # now 30.1 old
        await cache.get_offer(REF_A)  # expired -> re-fetch

        assert len(inner.get_calls) == 2

    async def test_manual_invalidate_all(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A), REF_B.key: _offer(REF_B)})
        cache = _cache(inner)

        await cache.get_offer(REF_A)
        await cache.get_offer(REF_B)

        evicted = cache.invalidate()
        assert evicted == 2
        assert cache.stats.entries == 0

        await cache.get_offer(REF_A)
        assert len(inner.get_calls) == 3

    async def test_manual_invalidate_single_ref(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A), REF_B.key: _offer(REF_B)})
        cache = _cache(inner)

        await cache.get_offer(REF_A)
        await cache.get_offer(REF_B)

        assert cache.invalidate(REF_A) == 1
        await cache.get_offer(REF_A)  # re-fetched
        await cache.get_offer(REF_B)  # aged out by the epoch bump -> re-fetch
        assert inner.get_calls.count(REF_A) == 2
        assert inner.get_calls.count(REF_B) == 2  # conservative epoch semantics

    async def test_invalidate_missing_ref(self) -> None:
        cache = _cache(FakeInner())
        assert cache.invalidate(REF_A) == 0
        assert cache.stats.invalidations == 1


class TestPortCompatibility:
    async def test_cache_satisfies_catalog_repository_port(self) -> None:
        """mypy-checked structural conformance: services can take the cache."""
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        repo: CatalogRepository = _cache(inner)

        offer = await repo.get_offer(REF_A)
        assert offer is not None
        assert await repo.upsert_entry(_spec(location="nbg1")) is True
        await repo.set_offer_enabled(REF_A, False)
        reloaded = await repo.get_offer(REF_A)
        assert reloaded is not None and not reloaded.enabled


class TestStats:
    async def test_stats_counter_accounting(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        cache = _cache(inner)

        await cache.get_offer(REF_A)  # miss
        await cache.get_offer(REF_A)  # hit
        await cache.get_offer(REF_A)  # hit
        cache.invalidate()

        stats = cache.stats
        assert stats.misses == 1
        assert stats.hits == 2
        assert stats.invalidations == 1
        assert stats.entries == 0


class TestRowIdPreserved:
    async def test_cached_offer_keeps_catalog_row_id(self) -> None:
        inner = FakeInner({REF_A.key: _offer(REF_A)})
        cache = _cache(inner)
        offer = await cache.get_offer(REF_A)
        assert offer is not None
        assert offer.id == ROW_ID
        assert isinstance(offer.id, UUID)
