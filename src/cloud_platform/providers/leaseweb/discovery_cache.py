"""Opt-in READ-ONLY discovery negative cache, never a checkout/mutation cache.

Entries preserve typed failures (not empty inventories), bounded across workers
by one Redis index. Epoch keys fence invalidation against in-flight requests.
Credentials are represented only by a fingerprint inside a hashed cache key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from cloud_platform.observability.catalog import discovery_active, probe_cache
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAuthenticationError,
    LeasewebForbiddenError,
    LeasewebNotFoundError,
    LeasewebRateLimitError,
    LeasewebServerError,
    LeasewebUnavailableError,
    LeasewebValidationError,
)

logger = logging.getLogger(__name__)
PREFIX = "catalog:leaseweb:negative:v1:"
INDEX = PREFIX + "index"
ERRORS = {
    "authentication": LeasewebAuthenticationError,
    "forbidden": LeasewebForbiddenError,
    "detail_not_found": LeasewebNotFoundError,
    "invalid_region": LeasewebValidationError,
    "rate_limited": LeasewebRateLimitError,
    "unavailable": LeasewebUnavailableError,
}
_limit_loop: Any = None
_global_limit: asyncio.Semaphore | None = None
_account_limits: dict[str, asyncio.Semaphore] = {}


def discovery_limits(account: str) -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    global _limit_loop, _global_limit, _account_limits
    loop = asyncio.get_running_loop()
    if _limit_loop is not loop:
        _limit_loop, _global_limit, _account_limits = loop, asyncio.Semaphore(2), {}
    assert _global_limit is not None
    return _account_limits.setdefault(account, asyncio.Semaphore(1)), _global_limit


@asynccontextmanager
async def discovery_slot(account_id: str) -> AsyncIterator[None]:
    account, global_limit = discovery_limits(account_id)
    async with account, global_limit:
        discovery_active.inc()
        try:
            yield
        finally:
            discovery_active.dec()


ADMIT = """
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',ARGV[1])
if redis.call('ZSCORE',KEYS[1],KEYS[2]) == false and redis.call('ZCARD',KEYS[1]) >= 4096 then
 return 0
end
redis.call('SET',KEYS[2],ARGV[2],'EX',ARGV[3])
redis.call('ZADD',KEYS[1],ARGV[1]+ARGV[3],KEYS[2])
if redis.call('TTL',KEYS[1]) < tonumber(ARGV[3])*2 then
 redis.call('EXPIRE',KEYS[1],ARGV[3]*2)
end
return 1
"""


@dataclass(frozen=True)
class ProbeScope:
    account: str
    fingerprint: str
    base_url: str
    path: str
    region: str = ""
    parameters: str = ""

    @property
    def family(self) -> str:
        return "cloud" if self.path.startswith("/publicCloud/") else "ordering"


class RedisProbeCache:
    def __init__(self, redis: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.redis, self.clock = redis, clock

    async def key(self, scope: ProbeScope) -> str:
        root = PREFIX + "epoch:" + scope.account
        epochs = await self.redis.mget(root, root + ":" + scope.region)
        identity = (
            scope.account,
            scope.fingerprint,
            scope.base_url,
            scope.path,
            scope.region,
            scope.parameters,
            *(str(v or "0") for v in epochs),
        )
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return PREFIX + "entry:" + digest

    async def get(self, key: str) -> dict[str, Any] | None:
        value = await self.redis.get(key)
        if not value:
            return None
        record = json.loads(value)
        if (
            not isinstance(record, dict)
            or record.get("kind") not in ERRORS
            or not isinstance(record.get("retry_at"), (int, float))
            or not math.isfinite(record["retry_at"])
            or not isinstance(record.get("attempt"), int)
        ):
            raise ValueError("invalid discovery cache record")
        return record

    async def put(self, key: str, scope: ProbeScope, record: dict[str, Any], ttl: int) -> None:
        value = dict(record, account=scope.account, region=scope.region, family=scope.family)
        await self.redis.eval(ADMIT, 2, INDEX, key, int(self.clock()), json.dumps(value), ttl)

    async def clear(self, key: str) -> None:
        await self.redis.delete(key)
        await self.redis.zrem(INDEX, key)

    async def invalidate(self, account: str, region: str | None = None) -> int:
        # Increment BEFORE removal: requests already in flight cannot revive
        # a matching usable entry after this invalidation returns.
        epoch = PREFIX + "epoch:" + account + (":" + region if region is not None else "")
        await self.redis.incr(epoch)
        keys = await self.redis.zrange(INDEX, 0, -1)
        removed = 0
        if keys:
            values = await self.redis.mget(*keys)
            selected = []
            for key, value in zip(keys, values, strict=True):
                item = json.loads(value) if value else {}
                if item.get("account") == account and (
                    region is None or item.get("region") == region
                ):
                    selected.append(key)
            if selected:
                removed = await self.redis.delete(*selected)
                await self.redis.zrem(INDEX, *selected)
        return removed


def failure_kind(error: Exception, path: str, region: str = "") -> str | None:
    if isinstance(error, LeasewebAuthenticationError):
        return "authentication"
    if isinstance(error, LeasewebForbiddenError):
        return "forbidden"
    if isinstance(error, LeasewebValidationError):
        from cloud_platform.providers.leaseweb.cloud import is_region_filter_rejection

        details = getattr(getattr(error, "payload", None), "error_details", None)
        region_field = isinstance(details, dict) and any(
            str(field).strip().casefold() == "region" for field in details
        )
        # An image/storage validation that mentions a region is not evidence
        # that this account's region FILTER is invalid. Prefer field evidence.
        if (
            region
            and path.startswith("/publicCloud/")
            and (region_field or (not details and is_region_filter_rejection(error)))
        ):
            return "invalid_region"
    if isinstance(error, LeasewebNotFoundError) and path.startswith("/ordering/v1/products/vps/"):
        return "detail_not_found"
    if isinstance(error, LeasewebRateLimitError):
        return "rate_limited"
    if isinstance(error, (LeasewebServerError, LeasewebUnavailableError)):
        return "unavailable"
    return None


class DiscoveryPolicy:
    def __init__(
        self,
        cache: RedisProbeCache | None,
        *,
        ttl: int = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cache, self.ttl, self.clock = cache, ttl, clock
        self.backend_retry_at = 0.0

    def _backend_failed(self) -> None:
        self.backend_retry_at = self.clock() + 5
        probe_cache.labels("backend_error").inc()

    async def run(self, scope: ProbeScope, call: Callable[[], Awaitable[Any]]) -> Any:
        # One account request at a time within a run; catalog advisory locking
        # serializes catalog replicas. Shared Redis preserves negative results
        # across newly constructed adapters and independent inventory jobs.
        async with discovery_slot(scope.account):
            key, previous = None, None
            if self.cache is not None and self.ttl > 0 and self.clock() >= self.backend_retry_at:
                try:
                    key = await self.cache.key(scope)
                    previous = await self.cache.get(key)
                except Exception:
                    key, previous = None, None
                    self._backend_failed()
            if (
                previous
                and previous.get("kind") in ERRORS
                and previous.get("retry_at", 0) > self.clock()
            ):
                kind = previous["kind"]
                probe_cache.labels("hit").inc()
                # Keep the exception type visible to every existing caller.
                message = (
                    "cached invalid region filter"
                    if kind == "invalid_region"
                    else "cached discovery " + kind
                )
                raise ERRORS[kind](message)
            probe_cache.labels("miss").inc()
            try:
                result = await call()
            except Exception as error:
                kind = failure_kind(error, scope.path, scope.region)
                if kind and key and self.cache is not None:
                    attempt = min(10, int((previous or {}).get("attempt", 0)) + 1)
                    delay = self.ttl
                    if kind in {"unavailable", "rate_limited"}:
                        delay = min(300, 30 * 2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
                        reset = getattr(error, "reset_at_unix", None)
                        if reset is not None:
                            delay = max(delay, reset - self.clock())
                    try:
                        await self.cache.put(
                            key,
                            scope,
                            dict(kind=kind, attempt=attempt, retry_at=self.clock() + delay),
                            max(self.ttl, int(delay) + 1),
                        )
                    except Exception:
                        self._backend_failed()
                raise
            if key and self.cache is not None:
                try:
                    await self.cache.clear(key)
                except Exception:
                    self._backend_failed()
            return result


current_policy: ContextVar[DiscoveryPolicy | None] = ContextVar("leaseweb_discovery", default=None)


@asynccontextmanager
async def discovery_scope(
    redis_url: str, *, enabled: bool = True, ttl: int = 3600
) -> AsyncIterator[None]:
    from redis.asyncio import Redis

    redis = Redis.from_url(
        redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1
    )
    policy = DiscoveryPolicy(RedisProbeCache(redis) if enabled else None, ttl=ttl if enabled else 0)
    token = current_policy.set(policy)
    try:
        yield
    finally:
        current_policy.reset(token)
        try:
            await redis.aclose()
        except Exception:
            logger.warning("discovery cache connection cleanup failed")
