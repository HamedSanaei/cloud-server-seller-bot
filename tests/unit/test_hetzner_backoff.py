"""Deterministic tests for Hetzner 429 backoff policy and client retry."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.providers.errors import ProviderRateLimited
from cloud_platform.providers.hetzner.backoff import RateLimitBackoff, RateLimitPolicy
from cloud_platform.providers.hetzner.client import HetznerCloudProvider, RateLimitSnapshot


class TestRateLimitPolicy:
    def test_defaults(self) -> None:
        policy = RateLimitPolicy()
        assert policy.max_retries == 3
        assert policy.base_delay_seconds == 0.5
        assert policy.max_delay_seconds == 30.0
        assert policy.respect_reset_at is True

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"max_retries": -1}, "max_retries"),
            ({"base_delay_seconds": 0}, "base_delay_seconds"),
            (
                {"base_delay_seconds": 5.0, "max_delay_seconds": 1.0},
                "max_delay_seconds",
            ),
        ],
    )
    def test_validation(self, kwargs: dict[str, Any], match: str) -> None:
        with pytest.raises(ValueError, match=match):
            RateLimitPolicy(**kwargs)


def _recording_backoff(
    policy: RateLimitPolicy | None = None, now: float = 1000.0
) -> tuple[RateLimitBackoff, list[float]]:
    delays: list[float] = []

    async def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    backoff = RateLimitBackoff(policy, sleep_fn=record_sleep, clock_fn=lambda: now)
    return backoff, delays


class TestExponentialPath:
    async def test_doubles_from_base_and_caps(self) -> None:
        policy = RateLimitPolicy(base_delay_seconds=0.5, max_delay_seconds=2.0)
        backoff, delays = _recording_backoff(policy)
        snapshot = RateLimitSnapshot(None, None, None)
        for attempt in range(4):
            awaited = await backoff.wait_before_retry(attempt, snapshot)
        assert awaited == min(0.5 * 2**3, 2.0)
        assert delays == [0.5, 1.0, 2.0, 2.0]


class TestResetHonoringPath:
    async def test_waits_out_future_reset(self) -> None:
        backoff, delays = _recording_backoff(now=1000.0)
        snapshot = RateLimitSnapshot(limit=100, remaining=0, reset_at_unix=1002)
        delay = await backoff.wait_before_retry(0, snapshot)
        assert delay == 2.0
        assert delays == [2.0]

    async def test_past_reset_waits_zero(self) -> None:
        backoff, _delays = _recording_backoff(now=1000.0)
        snapshot = RateLimitSnapshot(None, None, reset_at_unix=900)
        assert await backoff.wait_before_retry(0, snapshot) == 0.0

    async def test_far_future_reset_clamped_to_max(self) -> None:
        policy = RateLimitPolicy(max_delay_seconds=5.0)
        backoff, _delays = _recording_backoff(policy, now=1000.0)
        snapshot = RateLimitSnapshot(None, None, reset_at_unix=2000)
        assert await backoff.wait_before_retry(0, snapshot) == 5.0


def _provider_with_transport(handler: Any, policy: RateLimitPolicy) -> HetznerCloudProvider:
    provider = HetznerCloudProvider(token="test-token", rate_limit_policy=policy)
    transport_client = httpx.AsyncClient(
        base_url="https://api.hetzner.cloud/v1", transport=httpx.MockTransport(handler)
    )
    provider._client = transport_client
    return provider


def _429_handler(reset: str | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"RateLimit-Limit": "100", "RateLimit-Remaining": "0"}
        if reset is not None:
            headers["RateLimit-Reset"] = reset
        return httpx.Response(429, headers=headers, json={"error": {"message": "rate limit"}})

    return handler


class TestClientRetryIntegration:
    async def test_retries_then_succeeds(self) -> None:
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            if len(calls) < 3:
                return httpx.Response(429, json={"error": {"message": "rate limit"}})
            return httpx.Response(
                200,
                headers={"RateLimit-Remaining": "99"},
                json={"locations": []},
            )

        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        provider = HetznerCloudProvider(token="t")
        provider._backoff = RateLimitBackoff(RateLimitPolicy(), sleep_fn=fake_sleep)
        provider._client = httpx.AsyncClient(
            base_url="https://api.hetzner.cloud/v1", transport=httpx.MockTransport(handler)
        )

        result = await provider._request("GET", "/locations")

        assert result == {"locations": []}
        assert len(calls) == 3
        # No RateLimit-Reset header on 429s -> exponential path: 0.5 then 1.0.
        assert sleeps == [0.5, 1.0]
        assert provider.last_rate_limit.remaining == 99
        await provider.close()

    async def test_exhaustion_raises_rate_limited(self) -> None:
        policy = RateLimitPolicy(max_retries=2)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        provider = HetznerCloudProvider(token="t", rate_limit_policy=policy)
        provider._backoff = RateLimitBackoff(policy, sleep_fn=fake_sleep)
        provider._client = httpx.AsyncClient(
            base_url="https://api.hetzner.cloud/v1",
            transport=httpx.MockTransport(_429_handler(reset="995")),
        )

        with pytest.raises(ProviderRateLimited):
            await provider._request("GET", "/locations")

        assert len(sleeps) == policy.max_retries  # one wait per retry
        assert provider.last_rate_limit.reset_at_unix == 995
        await provider.close()

    async def test_zero_retries_single_attempt(self) -> None:
        policy = RateLimitPolicy(max_retries=0)
        sleep_mock = AsyncMock()
        provider = HetznerCloudProvider(token="t", rate_limit_policy=policy)
        provider._backoff = RateLimitBackoff(policy, sleep_fn=sleep_mock)
        provider._client = httpx.AsyncClient(
            base_url="https://api.hetzner.cloud/v1",
            transport=httpx.MockTransport(_429_handler()),
        )

        with pytest.raises(ProviderRateLimited):
            await provider._request("GET", "/locations")

        sleep_mock.assert_not_awaited()
        await provider.close()
