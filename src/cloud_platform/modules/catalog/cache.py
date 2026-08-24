"""Safe in-memory cache for the customer catalog (M04-008).

:class:`CatalogCache` decorates a :class:`CatalogRepository` and implements
the same port, so it can be swapped in at the wiring point: every catalog
reader uses the cache, and every catalog writer (price ingestion upserts,
offer visibility changes) goes through it as well and invalidates the cached
data. A reader therefore never serves a stale offer, price or visibility
state caused by a write that flows through the platform.

Safety properties:

- **Epoch invalidation** — every mutation (``upsert_entry``,
  ``set_offer_enabled``) bumps an internal epoch; cached entries tagged with
  an older epoch are treated as misses. This covers "cache invalidates on
  offer/price change" for all in-process writers.
- **TTL safety net** — even if a writer bypasses the cache (a direct
  database write from another process or a migration), entries expire after
  ``ttl_seconds``. ``ttl_seconds=0`` disables caching entirely (the wrapper
  then only provides invalidation hooks and statistics).
- **Negative caching** — unknown offers are cached like any other result so
  repeated lookups of missing offers do not hammer the store; they expire
  with the TTL and are dropped on every invalidation.
- **No stale write-through** — if an invalidation happens while a fetch is
  in flight, the fetched value is returned for that call but NOT cached; the
  next read fetches again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from cloud_platform.modules.catalog.domain import (
    CatalogEntrySpec,
    CatalogRepository,
    OfferRef,
    OfferState,
)


@dataclass(frozen=True, slots=True)
class _Entry:
    value: OfferState | None
    epoch: int
    loaded_at: float


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Point-in-time cache counters (for logs/monitoring)."""

    hits: int
    misses: int
    invalidations: int
    entries: int


class CatalogCache:
    """Caching decorator for the catalog repository port.

    The cache is single-event-loop scoped: all state transitions happen
    without yielding, so no locking is required within one process. Use one
    instance per process and wrap the shared repository so every service
    (create-server, visibility admin, catalog reads) sees the same data.
    """

    def __init__(
        self,
        inner: CatalogRepository,
        *,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be >= 0")
        self._inner = inner
        self._ttl = ttl_seconds
        self._clock = clock
        self._epoch = 0
        self._entries: dict[str, _Entry] = {}
        self._hits = 0
        self._misses = 0
        self._invalidations = 0

    # -- reads ---------------------------------------------------------------

    async def get_offer(self, ref: OfferRef) -> OfferState | None:
        """Cached offer lookup with epoch + TTL freshness checks."""
        now = self._clock()
        entry = self._entries.get(ref.key)
        if entry is not None and entry.epoch == self._epoch and (now - entry.loaded_at) < self._ttl:
            self._hits += 1
            return entry.value

        self._misses += 1
        epoch_before = self._epoch
        value = await self._inner.get_offer(ref)
        # A mutation may have bumped the epoch while the fetch was in flight;
        # the value is valid for this call but must not outlive the mutation.
        if self._ttl > 0 and self._epoch == epoch_before:
            self._entries[ref.key] = _Entry(value, self._epoch, self._clock())
        return value

    # -- writes (invalidate) ---------------------------------------------------

    async def upsert_entry(self, spec: CatalogEntrySpec) -> bool:
        """Persist a catalog row, then invalidate all cached offers."""
        created = await self._inner.upsert_entry(spec)
        self.invalidate()
        return created

    async def set_offer_enabled(self, ref: OfferRef, enabled: bool) -> None:
        """Change visibility, then invalidate all cached offers."""
        await self._inner.set_offer_enabled(ref, enabled)
        self.invalidate()

    # -- manual invalidation ---------------------------------------------------

    def invalidate(self, ref: OfferRef | None = None) -> int:
        """Evict one offer (or the whole cache) and return the count evicted.

        The epoch is bumped in both cases so in-flight fetches age out; as a
        consequence a per-ref invalidation also ages out other cached entries
        (they re-fetch on next read rather than serving potentially stale
        data). Correctness over hit ratio: catalogs are small.
        """
        self._epoch += 1
        self._invalidations += 1
        if ref is None:
            evicted = len(self._entries)
            self._entries.clear()
            return evicted
        entry = self._entries.pop(ref.key, None)
        return 1 if entry is not None else 0

    @property
    def stats(self) -> CacheStats:
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            invalidations=self._invalidations,
            entries=len(self._entries),
        )
