"""Provider retry/backoff policy with explicit error classification (M11-003).

Every provider call that may fail transiently should run through a
:class:`RetryExecutor`. The classification is explicit by design:

- PERMANENT errors (bad credentials, unknown resource, state conflict,
  unadvertised operation) are raised immediately — retrying cannot help.
- RETRYABLE errors (rate limits, 5xx/unavailability, timeouts, and any
  error not in the permanent set) are retried with exponential backoff,
  honoring a rate-limit reset epoch when the provider supplies one.

Sleep, clock, and randomness are injectable so tests never wait and are
deterministic.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from cloud_platform.providers.base import UnsupportedGatewayOperation
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderUnavailable,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

__all__ = [
    "PERMANENT_ERROR_TYPES",
    "RETRYABLE_ERROR_TYPES",
    "ErrorClass",
    "RetryExecutor",
    "RetryPolicy",
    "classify_provider_error",
]


class ErrorClass(StrEnum):
    """Whether a failed provider attempt should be retried."""

    RETRYABLE = "retryable"
    PERMANENT = "permanent"


#: Errors that retrying can never fix — raised immediately.
PERMANENT_ERROR_TYPES: tuple[type[ProviderError], ...] = (
    ProviderAuthError,
    ProviderNotFound,
    ProviderConflict,
    ProviderOutcomeUnknown,
    UnsupportedGatewayOperation,
)

#: Known transient provider errors (subset of the retryable set).
RETRYABLE_ERROR_TYPES: tuple[type[ProviderError], ...] = (
    ProviderRateLimited,
    ProviderUnavailable,
)


def classify_provider_error(error: Exception) -> ErrorClass:
    """Classify an error: the known permanent set is PERMANENT, everything
    else (known transient errors, timeouts, connection errors, unknowns) is
    RETRYABLE and bounded by the policy's attempt cap."""
    if isinstance(error, PERMANENT_ERROR_TYPES):
        return ErrorClass.PERMANENT
    return ErrorClass.RETRYABLE


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounds for a retrying provider operation.

    ``max_attempts`` is the total number of attempts (not retries).
    """

    max_attempts: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    jitter: float = 0.1  # delay is scaled into [1 - jitter, 1]

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if not 0.0 <= self.jitter < 1.0:
            raise ValueError("jitter must be in [0, 1)")


class RetryExecutor:
    """Executes a provider operation with classification-aware backoff."""

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_fn: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._policy = policy or RetryPolicy()
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._rng = rng or random.Random()

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    def compute_delay(self, attempt: int, error: Exception) -> float:
        """Delay before retry number ``attempt`` (zero-based) after ``error``.

        A rate-limited error carrying a reset epoch waits out the reset
        window (clamped); otherwise exponential backoff with optional
        bounded jitter applies.
        """
        if isinstance(error, ProviderRateLimited) and error.reset_at_unix is not None:
            remaining = error.reset_at_unix - self._clock_fn()
            return min(max(remaining, 0.0), self._policy.max_delay_seconds)
        delay = min(
            self._policy.base_delay_seconds * (2.0**attempt),
            self._policy.max_delay_seconds,
        )
        if self._policy.jitter > 0:
            delay *= 1.0 - self._policy.jitter + self._rng.random() * self._policy.jitter
        return delay

    async def execute(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run ``operation``, retrying only retryable-classified errors.

        Permanent errors propagate immediately. When the attempt budget is
        exhausted the last retryable error propagates.
        """
        last_error: Exception | None = None
        for attempt in range(1, self._policy.max_attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                if classify_provider_error(exc) is ErrorClass.PERMANENT:
                    logger.warning("permanent provider error; not retrying: %s", exc)
                    raise
                last_error = exc
                if attempt == self._policy.max_attempts:
                    logger.warning(
                        "provider operation exhausted %d attempts; last error: %s",
                        self._policy.max_attempts,
                        exc,
                    )
                    break
                delay = self.compute_delay(attempt - 1, exc)
                logger.info(
                    "retrying provider operation in %.2fs (attempt %d/%d): %s",
                    delay,
                    attempt,
                    self._policy.max_attempts,
                    exc,
                )
                await self._sleep_fn(delay)
        # Unreachable: the loop either returns or raises.
        raise last_error  # type: ignore[misc]
