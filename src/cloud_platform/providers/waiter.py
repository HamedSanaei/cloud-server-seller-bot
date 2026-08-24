"""Provider action waiter strategy (M07-004).

Provider mutations are asynchronous: the HTTP response means "accepted", not
"done". This module provides the shared strategy for actively waiting on an
action's completion:

- **Bounded backoff**: polls start at ``base_delay_seconds`` and grow by
  ``multiplier`` per pending poll, clamped at ``max_delay_seconds``; the total
  wait is bounded by ``max_wait_seconds``.
- **Rate-limit awareness**: every individual poll runs through a
  :class:`~cloud_platform.providers.retry.RetryExecutor`, so a 429 carrying a
  reset epoch waits out the reset window (clamped) and 5xx/unavailability are
  retried with bounded exponential backoff *within* the poll — the poll
  budget is not burned by transient provider hiccups.
- **Timeout is not failure**: hitting the deadline returns ``TIMEOUT`` so the
  caller (worker/reconciler) can keep watching via the normal reconciliation
  path instead of failing a resource that may still complete.
- Permanent errors (bad credentials, unknown resource) propagate to the
  caller, which decides how to contain them.

Sleep, clock, and the per-poll retry executor are injectable so tests never
wait and are deterministic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from cloud_platform.providers.retry import RetryExecutor, RetryPolicy

__all__ = [
    "ActionWaiter",
    "WaitOutcome",
    "WaitPolicy",
    "WaitProbe",
    "WaitResult",
    "WaitState",
]


class WaitState(StrEnum):
    """The state observed by one poll of the provider."""

    PENDING = "pending"  # provider is still working on the action
    COMPLETED = "completed"
    FAILED = "failed"  # provider reported a terminal failure for the action


class WaitOutcome(StrEnum):
    """The outcome of a wait. ``TIMEOUT`` means "still pending at deadline"."""

    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class WaitProbe:
    """One observation of the action's state (detail is provider text)."""

    state: WaitState
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class WaitResult:
    """Outcome of :meth:`ActionWaiter.wait_for`."""

    outcome: WaitOutcome
    polls: int
    elapsed_seconds: float
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class WaitPolicy:
    """Bounds for an action wait (all times in seconds)."""

    max_wait_seconds: float = 600.0
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be > 0")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")


class ActionWaiter:
    """Polls a provider action with bounded backoff until terminal or deadline."""

    def __init__(
        self,
        policy: WaitPolicy | None = None,
        *,
        retry_executor: RetryExecutor | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy or WaitPolicy()
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        # Per-poll retries share the policy's delay cap so a reset-epoch wait
        # can never exceed one poll's budget.
        self._retry = retry_executor or RetryExecutor(
            RetryPolicy(
                max_attempts=3,
                base_delay_seconds=self._policy.base_delay_seconds,
                max_delay_seconds=self._policy.max_delay_seconds,
                jitter=0.0,
            )
        )

    @property
    def policy(self) -> WaitPolicy:
        return self._policy

    async def wait_for(self, probe: Callable[[], Awaitable[WaitProbe]]) -> WaitResult:
        """Poll ``probe`` until it reports a terminal state or the deadline.

        At least one probe always runs. ``PENDING`` observations sleep with
        bounded exponential backoff; ``COMPLETED``/``FAILED`` return
        immediately; exceeding ``max_wait_seconds`` returns ``TIMEOUT``.
        Permanent provider errors raised by a probe propagate.
        """
        started = self._clock_fn()
        delay = self._policy.base_delay_seconds
        polls = 0
        while True:
            result_probe = await self._retry.execute(probe)
            polls += 1
            if result_probe.state is WaitState.COMPLETED:
                return WaitResult(
                    WaitOutcome.COMPLETED, polls, self._clock_fn() - started, result_probe.detail
                )
            if result_probe.state is WaitState.FAILED:
                return WaitResult(
                    WaitOutcome.FAILED, polls, self._clock_fn() - started, result_probe.detail
                )
            elapsed = self._clock_fn() - started
            if elapsed >= self._policy.max_wait_seconds:
                return WaitResult(WaitOutcome.TIMEOUT, polls, elapsed, result_probe.detail)
            sleep_time = min(delay, self._policy.max_wait_seconds - elapsed)
            await self._sleep_fn(max(sleep_time, 0.0))
            delay = min(delay * self._policy.multiplier, self._policy.max_delay_seconds)
