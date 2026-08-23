"""Tests for the provider retry/backoff policy: explicit retryable vs permanent (M11-003)."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable

import pytest

from cloud_platform.providers.base import UnsupportedGatewayOperation
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.retry import (
    ErrorClass,
    RetryExecutor,
    RetryPolicy,
    classify_provider_error,
)


class TestClassification:
    def test_permanent_errors(self) -> None:
        assert classify_provider_error(ProviderAuthError("bad token")) is ErrorClass.PERMANENT
        assert classify_provider_error(ProviderNotFound("missing")) is ErrorClass.PERMANENT
        assert classify_provider_error(ProviderConflict("busy")) is ErrorClass.PERMANENT
        assert (
            classify_provider_error(UnsupportedGatewayOperation("no refund"))
            is ErrorClass.PERMANENT
        )

    def test_known_retryable_errors(self) -> None:
        assert classify_provider_error(ProviderRateLimited("429", 123)) is ErrorClass.RETRYABLE
        assert classify_provider_error(ProviderUnavailable("503")) is ErrorClass.RETRYABLE

    def test_unknown_errors_are_retryable_and_bounded(self) -> None:
        assert classify_provider_error(TimeoutError()) is ErrorClass.RETRYABLE
        assert classify_provider_error(ConnectionError("reset")) is ErrorClass.RETRYABLE
        assert classify_provider_error(RuntimeError("weird")) is ErrorClass.RETRYABLE


class TestPolicyValidation:
    def test_defaults_are_valid(self) -> None:
        RetryPolicy()

    def test_zero_attempts_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            RetryPolicy(max_attempts=0)

    def test_nonpositive_base_delay_rejected(self) -> None:
        with pytest.raises(ValueError, match="base_delay"):
            RetryPolicy(base_delay_seconds=0.0)

    def test_max_below_base_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_delay"):
            RetryPolicy(base_delay_seconds=5.0, max_delay_seconds=1.0)

    def test_jitter_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="jitter"):
            RetryPolicy(jitter=1.0)
        with pytest.raises(ValueError, match="jitter"):
            RetryPolicy(jitter=-0.1)


class TestExecutor:
    def _executor(
        self,
        *,
        policy: RetryPolicy,
        delays: list[float],
        now: list[float] | None = None,
        seed: int = 7,
    ) -> RetryExecutor:
        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        return RetryExecutor(
            policy,
            sleep_fn=fake_sleep,
            clock_fn=lambda: now[0] if now is not None else 0.0,
            rng=random.Random(seed),
        )

    async def test_success_first_try_no_sleep(self) -> None:
        delays: list[float] = []
        executor = self._executor(policy=RetryPolicy(), delays=delays)
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        assert await executor.execute(op) == "ok"
        assert calls == 1
        assert delays == []

    async def test_retryable_then_success(self) -> None:
        delays: list[float] = []
        executor = self._executor(
            policy=RetryPolicy(max_attempts=3, base_delay_seconds=1.0, jitter=0.0), delays=delays
        )
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise ProviderUnavailable("503")
            return "done"

        assert await executor.execute(op) == "done"
        assert calls == 3
        # exponential: 1s then 2s
        assert delays == [1.0, 2.0]

    async def test_permanent_error_raises_immediately_without_retry(self) -> None:
        delays: list[float] = []
        executor = self._executor(policy=RetryPolicy(max_attempts=5), delays=delays)
        calls = 0

        async def op() -> None:
            nonlocal calls
            calls += 1
            raise ProviderAuthError("invalid credentials")

        with pytest.raises(ProviderAuthError):
            await executor.execute(op)
        assert calls == 1  # never retried
        assert delays == []  # never slept

    async def test_conflict_is_permanent(self) -> None:
        delays: list[float] = []
        executor = self._executor(policy=RetryPolicy(max_attempts=5), delays=delays)
        calls = 0

        async def op() -> None:
            nonlocal calls
            calls += 1
            raise ProviderConflict("already in use")

        with pytest.raises(ProviderConflict):
            await executor.execute(op)
        assert calls == 1

    async def test_retryable_exhaustion_raises_last_error(self) -> None:
        delays: list[float] = []
        executor = self._executor(
            policy=RetryPolicy(max_attempts=4, base_delay_seconds=1.0, jitter=0.0), delays=delays
        )
        calls = 0

        async def op() -> None:
            nonlocal calls
            calls += 1
            raise ProviderUnavailable("still down")

        with pytest.raises(ProviderUnavailable):
            await executor.execute(op)
        assert calls == 4  # max_attempts total
        assert delays == [1.0, 2.0, 4.0]  # max_attempts - 1 sleeps

    async def test_rate_limit_resets_delay_to_reset_window(self) -> None:
        delays: list[float] = []
        now = [100.0]
        executor = self._executor(
            policy=RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=30.0),
            delays=delays,
            now=now,
        )
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderRateLimited("429", reset_at_unix=115)  # 15s out
            return "ok"

        assert await executor.execute(op) == "ok"
        # Waits out the 15s reset window, not the 1s base backoff.
        assert delays == [15.0]

    async def test_rate_limit_reset_clamped_to_max_delay(self) -> None:
        delays: list[float] = []
        now = [100.0]
        executor = self._executor(
            policy=RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=30.0),
            delays=delays,
            now=now,
        )
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderRateLimited("429", reset_at_unix=500)  # 400s out -> clamp
            return "ok"

        assert await executor.execute(op) == "ok"
        assert delays == [30.0]

    async def test_delay_capped_by_max_delay(self) -> None:
        delays: list[float] = []
        executor = self._executor(
            policy=RetryPolicy(
                max_attempts=10, base_delay_seconds=10.0, max_delay_seconds=25.0, jitter=0.0
            ),
            delays=delays,
        )
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls <= 5:
                raise ProviderUnavailable("503")
            return "ok"

        await executor.execute(op)
        # 10, 20, 40->25, 80->25, 160->25
        assert delays == [10.0, 20.0, 25.0, 25.0, 25.0]

    async def test_jitter_keeps_delay_in_bounds(self) -> None:
        delays: list[float] = []
        policy = RetryPolicy(
            max_attempts=2, base_delay_seconds=10.0, max_delay_seconds=100.0, jitter=0.2
        )
        executor = self._executor(policy=policy, delays=delays, seed=42)
        calls = 0

        async def op() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderUnavailable("503")
            return "ok"

        await executor.execute(op)
        assert len(delays) == 1
        # delay = 10 * uniform[0.8, 1.0]
        assert 8.0 <= delays[0] <= 10.0

    async def test_zero_jitter_is_deterministic(self) -> None:
        policy = RetryPolicy(max_attempts=3, base_delay_seconds=2.0, jitter=0.0)

        def make_op(calls: list[int]) -> Callable[[], Awaitable[None]]:
            async def op() -> None:
                calls.append(1)
                raise ProviderUnavailable("503")

            return op

        delays_a: list[float] = []
        delays_b: list[float] = []
        executor_a = self._executor(policy=policy, delays=delays_a, seed=1)
        executor_b = self._executor(policy=policy, delays=delays_b, seed=999)

        for executor in (executor_a, executor_b):
            with pytest.raises(ProviderUnavailable):
                await executor.execute(make_op([]))

        # Identical delays despite different rng seeds.
        assert delays_a == delays_b == [2.0, 4.0]
