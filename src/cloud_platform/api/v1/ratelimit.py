"""Per-user/token rate limiting for REST v1 (M14-006).

Acceptance: per-user/token limits with headers.

A sliding-window limiter keyed by the authenticated identity (the API
token's own id - distinct tokens of the SAME user get INDEPENDENT
buckets). Every v1 response carries ``X-RateLimit-Limit``,
``X-RateLimit-Remaining`` and ``X-RateLimit-Reset``; exceeding the limit
returns the stable ``429 rate_limited`` envelope plus ``Retry-After``.

The limiter is in-process (modular monolith: one API process per replica,
so this bounds each process fairly); it never persists anything.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateDecision:
    """Outcome of one rate-limit check."""

    allowed: bool
    remaining: int
    #: Approximate unix epoch second at which the window frees up.
    reset_epoch: int
    #: Seconds the client should wait before retrying (0 when allowed).
    retry_after: int


class SlidingWindowRateLimiter:
    """In-memory sliding window; bounded memory via LRU eviction."""

    def __init__(
        self,
        limit_per_window: int,
        *,
        window_seconds: int = 60,
        max_tracked_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._limit = limit_per_window
        self._window = window_seconds
        self._max_keys = max_tracked_keys
        self._clock = clock
        self._wall_clock = wall_clock
        # key -> deque(monotonic timestamps); OrderedDict gives cheap LRU touch
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def limit(self) -> int:
        return self._limit

    def check(self, key: str) -> RateDecision:
        """Account one request for ``key`` and decide allow/deny."""
        now = self._clock()
        bucket = self._hits.get(key)
        if bucket is not None:
            self._hits.move_to_end(key)
            cutoff = now - self._window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
        else:
            bucket = deque()
            self._hits[key] = bucket
            self._evict_if_needed()

        if len(bucket) >= self._limit:
            oldest = bucket[0]
            retry_after = max(1, math.ceil(oldest + self._window - now))
            return RateDecision(
                allowed=False,
                remaining=0,
                reset_epoch=int(self._wall_clock()) + retry_after,
                retry_after=retry_after,
            )
        bucket.append(now)
        remaining = self._limit - len(bucket)
        return RateDecision(
            allowed=True,
            remaining=remaining,
            reset_epoch=int(self._wall_clock()) + self._window,
            retry_after=0,
        )

    def _evict_if_needed(self) -> None:
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)
