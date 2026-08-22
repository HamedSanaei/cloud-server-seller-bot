"""Rate-limit backoff policy for the Hetzner adapter.

Computes bounded, deterministic delays between retries of HTTP 429 responses,
honoring the provider's ``RateLimit-Reset`` epoch when available. The sleep
function is injectable so workers and tests can run without real waiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloud_platform.providers.hetzner.client import RateLimitSnapshot

__all__ = ["RateLimitBackoff", "RateLimitPolicy"]


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Bounds for 429 retry behavior.

    A request is attempted at most ``1 + max_retries`` times before the final
    :class:`ProviderRateLimited` propagates to the caller.
    """

    max_retries: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    respect_reset_at: bool = True

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")


class RateLimitBackoff:
    """Waits an appropriate delay before a rate-limited retry."""

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_fn: Callable[[], float] = time.time,
    ) -> None:
        self._policy = policy or RateLimitPolicy()
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn

    @property
    def policy(self) -> RateLimitPolicy:
        return self._policy

    def compute_delay(self, attempt: int, snapshot: RateLimitSnapshot) -> float:
        """Compute the delay in seconds before retry number ``attempt``.

        ``attempt`` is zero-based for the first retry. When the snapshot carries
        a reset epoch and the policy honors it, the delay waits out the reset
        window (clamped to ``[0, max_delay_seconds]``); otherwise exponential
        backoff applies.
        """
        if self._policy.respect_reset_at and snapshot.reset_at_unix is not None:
            remaining = snapshot.reset_at_unix - self._clock_fn()
            return min(max(remaining, 0.0), self._policy.max_delay_seconds)
        factor = float(2**attempt)
        return min(self._policy.base_delay_seconds * factor, self._policy.max_delay_seconds)

    async def wait_before_retry(self, attempt: int, snapshot: RateLimitSnapshot) -> float:
        """Compute the delay, sleep for it, and return the awaited duration."""
        delay = self.compute_delay(attempt, snapshot)
        await self._sleep_fn(delay)
        return delay
