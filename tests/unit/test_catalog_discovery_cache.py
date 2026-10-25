"""Read-only negative-cache regression without credentials/provider network."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.observability.catalog import http_attempts
from cloud_platform.providers.leaseweb.discovery_cache import (
    DiscoveryPolicy,
    ProbeScope,
    RedisProbeCache,
    current_policy,
)
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAuthenticationError,
    LeasewebForbiddenError,
    LeasewebNotFoundError,
    LeasewebRateLimitError,
    LeasewebUnavailableError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.transport import LeasewebTransport, Throttle


class MemoryCache:
    def __init__(self):
        self.rows = {}

    async def key(self, scope):
        return repr(scope)

    async def get(self, key):
        return self.rows.get(key)

    async def put(self, key, scope, value, ttl):
        self.rows[key] = value

    async def clear(self, key):
        self.rows.pop(key, None)


SCOPE = ProbeScope(
    "account-a", "fingerprint-a", "https://provider.test", "/publicCloud/v1/images", "eu-west-1"
)


@pytest.mark.parametrize(
    "error",
    [
        LeasewebAuthenticationError("no auth"),
        LeasewebForbiddenError("no access"),
        LeasewebValidationError("invalid region"),
        LeasewebNotFoundError("no detail"),
    ],
)
async def test_definitive_failures_survive_adapter_recreation_but_not_ttl_or_rotation(error):
    now = [1000]
    scope = (
        replace(SCOPE, path="/ordering/v1/products/vps/product-1")
        if isinstance(error, LeasewebNotFoundError)
        else SCOPE
    )
    cache = MemoryCache()
    call = AsyncMock(side_effect=error)
    for _ in range(3):
        with pytest.raises(type(error)):
            await DiscoveryPolicy(cache, clock=lambda: now[0]).run(scope, call)
    assert call.await_count == 1
    for changed in (
        replace(scope, account="account-b"),
        replace(scope, region="other"),
        replace(scope, fingerprint="rotated"),
    ):
        with pytest.raises(type(error)):
            await DiscoveryPolicy(cache, clock=lambda: now[0]).run(changed, call)
    assert call.await_count == 4
    now[0] += 3600
    with pytest.raises(type(error)):
        await DiscoveryPolicy(cache, clock=lambda: now[0]).run(scope, call)
    assert call.await_count == 5


@pytest.mark.parametrize(
    "path",
    ["/ordering/v1/products/vps", "/ordering/v1/products/vps/VPS02_1"],
)
async def test_ordering_catalog_400_is_cached_only_for_same_discovery_scope(path):
    """A failing optional catalog endpoint must not be queried every 15 minutes."""
    now = [1000]
    scope = replace(SCOPE, path=path, region="FRA-10", parameters="billingCycle=1_MONTH")
    cache = MemoryCache()
    call = AsyncMock(side_effect=LeasewebValidationError("HTTP 400"))
    for _ in range(3):
        with pytest.raises(LeasewebValidationError):
            await DiscoveryPolicy(cache, clock=lambda: now[0]).run(scope, call)
    assert call.await_count == 1

    # Credential rotation, a different region, and different contract parameters
    # must never receive a previous catalog failure as evidence.
    for other in (
        replace(scope, fingerprint="new-credential"),
        replace(scope, region="AMS-01"),
        replace(scope, parameters="billingCycle=1_YEAR"),
    ):
        with pytest.raises(LeasewebValidationError):
            await DiscoveryPolicy(cache, clock=lambda: now[0]).run(other, call)
    assert call.await_count == 4

    # Bounded freshness: Leaseweb is retried after TTL and can recover without
    # waiting for a worker restart.
    now[0] += 3600
    call.side_effect = None
    call.return_value = {"products": []}
    assert await DiscoveryPolicy(cache, clock=lambda: now[0]).run(scope, call) == {"products": []}
    assert call.await_count == 5
    assert await cache.get(repr(scope)) is None


async def test_unrelated_validation_is_not_cached_or_converted_to_empty_inventory():
    cache = MemoryCache()
    call = AsyncMock(side_effect=LeasewebValidationError("invalid disk size"))
    for _ in range(2):
        with pytest.raises(LeasewebValidationError):
            await DiscoveryPolicy(cache).run(SCOPE, call)
    assert call.await_count == 2 and not cache.rows


async def test_image_field_region_message_and_unscoped_reads_are_not_region_negatives():
    from cloud_platform.providers.leaseweb.discovery_cache import failure_kind

    error = LeasewebValidationError("invalid region for selected image")
    error.payload = SimpleNamespace(error_details={"imageId": ("invalid region",)})
    assert failure_kind(error, SCOPE.path, SCOPE.region) is None
    error.payload = SimpleNamespace(error_details={"region": ("unknown value",)})
    assert failure_kind(error, SCOPE.path, SCOPE.region) == "invalid_region"
    assert failure_kind(error, SCOPE.path, "") is None


async def test_transient_backoff_and_retry_after_are_not_permanent_negatives(monkeypatch):
    monkeypatch.setattr(
        "cloud_platform.providers.leaseweb.discovery_cache.random.uniform", lambda a, b: 1
    )
    now = [1000]
    cache = MemoryCache()
    call = AsyncMock(side_effect=LeasewebUnavailableError("timeout"))
    policy = DiscoveryPolicy(cache, clock=lambda: now[0])
    for _ in range(2):
        with pytest.raises(LeasewebUnavailableError):
            await policy.run(SCOPE, call)
    assert call.await_count == 1
    now[0] += 30
    with pytest.raises(LeasewebUnavailableError):
        await policy.run(SCOPE, call)
    assert call.await_count == 2
    assert next(iter(cache.rows.values()))["retry_at"] == 1090
    call.side_effect = None
    call.return_value = {"inventory": "proven"}
    now[0] = 1090
    assert await policy.run(SCOPE, call) == {"inventory": "proven"}
    assert not cache.rows
    call.side_effect = LeasewebRateLimitError("rate", reset_at_unix=2000)
    with pytest.raises(LeasewebRateLimitError):
        await policy.run(SCOPE, call)
    assert next(iter(cache.rows.values()))["retry_at"] >= 2000


async def test_cache_failure_falls_back_to_reads_not_invented_success():
    cache = MemoryCache()
    cache.get = AsyncMock(side_effect=RuntimeError("Redis offline"))
    call = AsyncMock(side_effect=LeasewebForbiddenError("forbidden inventory"))
    with pytest.raises(LeasewebForbiddenError):
        await DiscoveryPolicy(cache).run(SCOPE, call)
    assert call.await_count == 1


async def test_global_and_account_limits_apply_across_policy_instances():
    active, peak = 0, 0
    accounts = {}

    async def read(account):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        accounts[account] = accounts.get(account, 0) + 1
        assert accounts[account] == 1 and active <= 2
        await asyncio.sleep(0.005)
        active -= 1
        accounts[account] -= 1

    await asyncio.gather(
        *(
            DiscoveryPolicy(None).run(
                replace(SCOPE, account=str(i % 3)), lambda i=i: read(str(i % 3))
            )
            for i in range(9)
        )
    )
    assert peak == 2


async def test_physical_http_attempts_count_retries_and_mutations_are_never_cached():
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(429 if count < 3 else 200, json={"ok": True})

    transport = LeasewebTransport(
        "fake-unit-test-key", base_url="https://provider.test", throttle=Throttle(wait=AsyncMock())
    )
    client = httpx.AsyncClient(
        base_url="https://provider.test", transport=httpx.MockTransport(handler)
    )
    await transport.aclose()
    transport.set_client(client)
    # A replaced test client still requires an explicitly fake auth header.
    client.headers["X-LSW-Auth"] = "fake-unit-test-key"
    operation = "GET /publicCloud/v1/images"
    before = http_attempts.labels("leaseweb", operation, "429")._value.get()
    token = current_policy.set(DiscoveryPolicy(MemoryCache()))
    try:
        assert await transport.request("GET", "/publicCloud/v1/images") == {"ok": True}
        assert count == 3
        assert http_attempts.labels("leaseweb", operation, "429")._value.get() - before == 2
        count = 0
        from cloud_platform.providers.leaseweb.errors import LeasewebAmbiguousMutationError

        with pytest.raises(LeasewebAmbiguousMutationError):
            await transport.request("POST", "/publicCloud/v1/instances", mutating=True)
        assert count == 1
    finally:
        current_policy.reset(token)
        await transport.aclose()


async def test_epoch_invalidation_fences_inflight_results_and_is_region_scoped():
    values = {}

    async def mget(*keys):
        return [values.get(key) for key in keys]

    async def incr(key):
        values[key] = int(values.get(key, 0)) + 1

    redis = SimpleNamespace(mget=mget, incr=incr, zrange=AsyncMock(return_value=[]))
    cache = RedisProbeCache(redis)
    original = await cache.key(SCOPE)
    other = replace(SCOPE, region="other")
    other_key = await cache.key(other)
    await cache.invalidate(SCOPE.account, SCOPE.region)
    assert await cache.key(SCOPE) != original
    assert await cache.key(other) == other_key
    await cache.invalidate(SCOPE.account)
    assert await cache.key(other) != other_key


@pytest.mark.parametrize("status", [429, 503])
async def test_long_retry_after_is_a_shared_cooldown_not_an_early_retry(status):
    import time

    from cloud_platform.providers.leaseweb.errors import LeasewebError

    wait = AsyncMock()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status, headers={"Retry-After": "3600"}, json={"errorMessage": "busy"}
        )

    transport = LeasewebTransport(
        "synthetic-key", base_url="https://provider.test", throttle=Throttle(wait=wait)
    )
    await transport.aclose()
    transport.set_client(
        httpx.AsyncClient(
            base_url="https://provider.test",
            headers={"X-LSW-Auth": "synthetic-key"},
            transport=httpx.MockTransport(handler),
        )
    )
    token = current_policy.set(DiscoveryPolicy(MemoryCache()))
    try:
        with pytest.raises(LeasewebError) as error:
            await transport.request("GET", "/publicCloud/v1/images")
        assert error.value.reset_at_unix >= time.time() + 3590
        with pytest.raises(LeasewebError):
            await transport.request("GET", "/publicCloud/v1/images")
        assert calls == 1
    finally:
        current_policy.reset(token)
        await transport.aclose()


async def test_timeout_retries_are_bounded_counted_and_cached_without_empty_inventory():
    from cloud_platform.providers.leaseweb.errors import LeasewebTimeoutError

    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    transport = LeasewebTransport(
        "synthetic-key", base_url="https://provider.test", throttle=Throttle(wait=AsyncMock())
    )
    await transport.aclose()
    transport.set_client(
        httpx.AsyncClient(
            base_url="https://provider.test",
            headers={"X-LSW-Auth": "synthetic-key"},
            transport=httpx.MockTransport(handler),
        )
    )
    token = current_policy.set(DiscoveryPolicy(MemoryCache()))
    try:
        with pytest.raises(LeasewebTimeoutError):
            await transport.request("GET", "/publicCloud/v1/instances")
        with pytest.raises(LeasewebUnavailableError):
            await transport.request("GET", "/publicCloud/v1/instances")
        assert calls == 3
    finally:
        current_policy.reset(token)
        await transport.aclose()


async def test_lowercase_auth_header_rotation_bypasses_old_negative_cache():
    calls = []

    async def handler(request):
        calls.append(request.headers["X-LSW-Auth"])
        return httpx.Response(403, json={"errorMessage": "synthetic denial"})

    transport = LeasewebTransport("synthetic-default-key", base_url="https://provider.test")
    await transport.aclose()
    transport.set_client(
        httpx.AsyncClient(
            base_url="https://provider.test",
            transport=httpx.MockTransport(handler),
        )
    )
    token = current_policy.set(DiscoveryPolicy(MemoryCache()))
    try:
        for key in ("synthetic-key-a", "synthetic-key-a", "synthetic-key-b", "synthetic-key-b"):
            with pytest.raises(LeasewebForbiddenError):
                await transport.request(
                    "GET", "/publicCloud/v1/instances", headers={"x-lsw-auth": key}
                )
        assert calls == ["synthetic-key-a", "synthetic-key-b"]
    finally:
        current_policy.reset(token)
        await transport.aclose()
