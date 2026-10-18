"""Safe HTTP-attempt benchmark: archived transport vs opt-in discovery cache.

Only MockTransport, synthetic keys, and in-memory Redis/Lua emulation. No network.
Run with uv run --with 'fakeredis[lua]==2.40.0' python -m scripts.benchmark_catalog_discovery.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx

from cloud_platform.providers.leaseweb.discovery_cache import (
    DiscoveryPolicy,
    RedisProbeCache,
    current_policy,
)
from cloud_platform.providers.leaseweb.errors import LeasewebError
from cloud_platform.providers.leaseweb.transport import LeasewebTransport, Throttle


async def sample(transport_type, *, cached: bool, cycles: int):
    calls = 0
    now = [1000]

    async def handler(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.002)  # identical synthetic I/O latency in both arms
        path = request.url.path
        if path.endswith("instances"):
            return httpx.Response(403, json={"errorMessage": "permission denied"})
        if path.endswith("images"):
            return httpx.Response(400, json={"errorMessage": "invalid region"})
        if "/products/vps/" in path:
            return httpx.Response(404, json={"errorMessage": "unknown optional product"})
        return httpx.Response(200, json={"inventory_generation": now[0]})

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = RedisProbeCache(redis, clock=lambda: now[0])
    token = current_policy.set(None)
    outcomes = []
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        for _ in range(cycles):
            current_policy.set(DiscoveryPolicy(cache, clock=lambda: now[0]) if cached else None)
            transport = transport_type(
                "synthetic-benchmark-key",
                base_url="https://provider.test",
                throttle=Throttle(wait=AsyncMock()),
            )
            await transport.aclose()
            transport.set_client(
                httpx.AsyncClient(
                    base_url="https://provider.test",
                    headers={"X-LSW-Auth": "synthetic-benchmark-key"},
                    transport=httpx.MockTransport(handler),
                )
            )
            try:
                for path in (
                    "/publicCloud/v1/instances",
                    "/publicCloud/v1/images",
                    "/ordering/v1/products/vps/optional",
                    "/publicCloud/v1/regions",
                ):
                    try:
                        outcomes.append(
                            await transport.request("GET", path, params={"region": "r"})
                        )
                    except LeasewebError as error:
                        outcomes.append(type(error).__name__)
            finally:
                await transport.aclose()
            now[0] += 900
        return dict(
            http_attempts=calls,
            wall_seconds=time.perf_counter() - wall,
            worker_cpu_seconds=time.process_time() - cpu,
            outcomes=outcomes,
        )
    finally:
        current_policy.reset(token)
        await redis.aclose()


async def run(args):
    spec = importlib.util.spec_from_file_location(
        "baseline_discovery_transport", args.baseline_transport
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("archived baseline transport required")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    report = {
        "repetitions": 5,
        "network": "MockTransport only",
        "cache": "fakeredis 2.40.0 Lua",
        "results": {},
    }
    for scenario, cycles in (("cold", 1), ("warm_and_changed_inventory", 4)):
        samples = {"baseline": [], "candidate": []}
        for repeat in range(5):
            order = [
                ("baseline", module.LeasewebTransport, False),
                ("candidate", LeasewebTransport, True),
            ]
            if repeat % 2:
                order.reverse()
            pair = {}
            for name, cls, cached in order:
                pair[name] = await sample(cls, cached=cached, cycles=cycles)
            assert pair["baseline"]["outcomes"] == pair["candidate"]["outcomes"]
            for name, value in pair.items():
                value.pop("outcomes")
                samples[name].append(value)
        summary = {
            name: {
                metric: dict(
                    median=statistics.median(v[metric] for v in values),
                    min=min(v[metric] for v in values),
                    max=max(v[metric] for v in values),
                )
                for metric in values[0]
            }
            for name, values in samples.items()
        }
        report["results"][scenario] = dict(
            summary=summary, samples=samples, typed_outcome_parity=True
        )
        print(json.dumps({"scenario": scenario, "summary": summary}), flush=True)
    await asyncio.to_thread(
        Path(args.output).write_text, json.dumps(report, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-transport", required=True)
    parser.add_argument("--output", default=".ci-artifacts/catalog-discovery-benchmark.json")
    asyncio.run(run(parser.parse_args()))
