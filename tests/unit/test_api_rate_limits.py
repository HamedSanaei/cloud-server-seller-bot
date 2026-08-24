"""Tests for API rate limits (M14-006).

Acceptance: per-user/token limits with headers.

- a sliding window keyed by the token's OWN id (distinct tokens of one
  user get independent buckets);
- every response carries X-RateLimit-Limit/Remaining/Reset;
- exceeding returns the stable 429 rate_limited envelope + Retry-After;
- the window slides: blocked identities recover as old hits expire.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_platform.api.v1 import router as v1_router
from cloud_platform.api.v1.dependencies import get_token_authentication
from cloud_platform.api.v1.ratelimit import SlidingWindowRateLimiter
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope

USER_ID = uuid4()


def make_auth(token_id: Any = None) -> TokenAuthentication:
    return TokenAuthentication(
        user_id=USER_ID,
        scopes=frozenset(TokenScope),
        token_id=token_id or uuid4(),
    )


class FakeCatalogRepo:
    async def list_offers(self) -> list[Any]:
        return []


class FakeClock:
    """Controllable monotonic + wall clocks."""

    def __init__(self) -> None:
        self.monotonic_now = 1_000.0
        self.wall_now = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_now

    def wall(self) -> float:
        return self.wall_now


def build_app(
    limit: int,
    auth: TokenAuthentication | None = None,
) -> tuple[TestClient, FakeClock]:
    clock = FakeClock()
    app = FastAPI()
    app.include_router(v1_router)
    from cloud_platform.api.v1.errors import install_error_handlers

    install_error_handlers(app)
    from cloud_platform.api.v1.router import _catalog_repo

    resolved_auth = auth or make_auth()  # ONE identity for all requests
    app.dependency_overrides[get_token_authentication] = lambda: resolved_auth
    app.dependency_overrides[_catalog_repo] = lambda: FakeCatalogRepo()
    app.state.rate_limiter = SlidingWindowRateLimiter(
        limit,
        window_seconds=60,
        clock=clock.monotonic,
        wall_clock=clock.wall,
    )
    return TestClient(app), clock  # type: ignore[return-value]


class TestHeadersAndLimits:
    def test_headers_present_on_success(self) -> None:
        client, _clock = build_app(limit=10)
        resp = client.get("/v1/catalog/offers")
        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "10"
        assert int(resp.headers["X-RateLimit-Remaining"]) == 9
        reset = int(resp.headers["X-RateLimit-Reset"])
        assert reset >= 1_700_000_000

    def test_exceeding_limit_returns_429_envelope(self) -> None:
        client, _clock = build_app(limit=2)
        assert client.get("/v1/catalog/offers").status_code == 200
        assert client.get("/v1/catalog/offers").status_code == 200
        third = client.get("/v1/catalog/offers")
        assert third.status_code == 429
        error = third.json()["error"]
        assert set(error) == {"code", "message", "details"}
        assert error["code"] == "rate_limited"
        retry_after = int(third.headers["Retry-After"])
        assert 1 <= retry_after <= 60
        # denied requests consume nothing further; remaining stays 0
        assert third.headers["X-RateLimit-Remaining"] == "0"

    def test_window_slides_and_recovers(self) -> None:
        client, clock = build_app(limit=1)
        assert client.get("/v1/catalog/offers").status_code == 200
        assert client.get("/v1/catalog/offers").status_code == 429
        clock.monotonic_now += 61  # past the window
        recovered = client.get("/v1/catalog/offers")
        assert recovered.status_code == 200
        assert int(recovered.headers["X-RateLimit-Remaining"]) == 0

    def test_distinct_tokens_get_independent_buckets(self) -> None:
        client, _clock = build_app(limit=1, auth=make_auth())
        # token A exhausts its bucket
        assert client.get("/v1/catalog/offers").status_code == 200
        assert client.get("/v1/catalog/offers").status_code == 429
        # token B has a fresh bucket despite the SAME user
        token_b = make_auth()
        client.app.dependency_overrides[get_token_authentication] = lambda: token_b  # type: ignore[attr-defined]
        resp_b = client.get("/v1/catalog/offers")
        assert resp_b.status_code == 200
        assert int(resp_b.headers["X-RateLimit-Remaining"]) == 0

    def test_reset_epoch_uses_wall_clock(self) -> None:
        client, _clock = build_app(limit=5)
        resp = client.get("/v1/catalog/offers")
        reset = int(resp.headers["X-RateLimit-Reset"])
        # window end is roughly wall-clock now + 60s
        assert abs(reset - (1_700_000_000 + 60)) <= 2


class TestLimiterUnit:
    def test_decision_math_without_http(self) -> None:
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(
            3, window_seconds=10, clock=clock.monotonic, wall_clock=clock.wall
        )
        decisions = [limiter.check("k") for _ in range(3)]
        assert all(d.allowed for d in decisions)
        assert [d.remaining for d in decisions] == [2, 1, 0]
        denied = limiter.check("k")
        assert not denied.allowed and denied.retry_after >= 1

    def test_lru_eviction_bounds_memory(self) -> None:
        limiter = SlidingWindowRateLimiter(5, max_tracked_keys=3)
        for i in range(10):
            limiter.check(f"k{i}")
        assert len(limiter._hits) <= 3

    def test_retry_after_counts_down_to_expiry(self) -> None:
        clock = FakeClock()
        limiter = SlidingWindowRateLimiter(
            1, window_seconds=30, clock=clock.monotonic, wall_clock=clock.wall
        )
        assert limiter.check("k").allowed
        denied = limiter.check("k")
        assert not denied.allowed
        clock.monotonic_now += 20
        still_denied = limiter.check("k")
        assert still_denied.retry_after == 10
