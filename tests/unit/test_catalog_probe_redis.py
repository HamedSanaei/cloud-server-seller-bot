"""Redis Lua/index protocol through an optional Redis-compatible emulator.

Run explicitly with: uv run --with 'fakeredis[lua]==2.40.0' pytest <this file>.
Not a claim of testing a deployed Redis server.
"""

from dataclasses import replace

import pytest

fakeredis = pytest.importorskip("fakeredis.aioredis")

from cloud_platform.providers.leaseweb.discovery_cache import (  # noqa: E402
    INDEX,
    DiscoveryPolicy,
    ProbeScope,
    RedisProbeCache,
)
from cloud_platform.providers.leaseweb.errors import LeasewebForbiddenError  # noqa: E402

SCOPE = ProbeScope(
    "a", "synthetic-fingerprint", "https://provider.test", "/publicCloud/v1/instances", "r"
)


async def test_lua_shared_cache_invalidation_ttl_and_namespace():
    redis = fakeredis.FakeRedis(decode_responses=True)
    cache = RedisProbeCache(redis)
    key = await cache.key(SCOPE)
    other = replace(SCOPE, region="other")
    other_key = await cache.key(other)
    record = dict(kind="forbidden", attempt=1, retry_at=10**12)
    await cache.put(key, SCOPE, record, 86400)
    await cache.put(other_key, other, record, 3600)
    assert await redis.ttl(INDEX) > 86400
    assert await cache.get(key) == dict(record, account="a", region="r", family="cloud")
    assert "synthetic-fingerprint" not in str(await redis.get(key))
    assert await cache.invalidate("a", "r") == 1
    assert await cache.get(key) is None and await cache.get(other_key) is not None
    assert await cache.key(other) == other_key
    await redis.expire(other_key, 0)
    assert await cache.get(other_key) is None
    await redis.aclose()


async def test_lua_capacity_limit_is_atomic_and_corruption_falls_back_to_read():
    redis = fakeredis.FakeRedis(decode_responses=True)
    cache = RedisProbeCache(redis)
    # Full index, not 4096 provider calls; the Lua admission check is real.
    await redis.zadd(INDEX, {f"synthetic-{i}": 10**12 for i in range(4096)})
    key = await cache.key(SCOPE)
    await cache.put(key, SCOPE, dict(kind="forbidden", attempt=1, retry_at=10**12), 3600)
    assert await redis.zcard(INDEX) == 4096 and await redis.get(key) is None
    await redis.set(key, '"corrupt-record"')
    calls = 0

    async def read():
        nonlocal calls
        calls += 1
        raise LeasewebForbiddenError("synthetic inventory denial")

    policy = DiscoveryPolicy(cache)
    for _ in range(2):
        with pytest.raises(LeasewebForbiddenError):
            await policy.run(SCOPE, read)
    assert calls == 2 and policy.backend_retry_at > 0
    await redis.aclose()
