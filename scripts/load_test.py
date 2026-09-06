"""Reproducible load test for the API and worker queues (M16-002).

Acceptance: **bottlenecks measured with reproducible script.**

Usage::

    uv run python scripts/load_test.py --requests 200 --users 8

The script drives the REAL v1 ASGI stack (auth chain, scopes, rate
limiter, error envelope) over in-memory fake data ports through
``httpx.ASGITransport`` - no Postgres, Redis or live providers - so runs
are reproducible anywhere. It also drains the REAL arq job coroutines
through an asyncio queue with a configurable consumer pool.

Outputs: an ASCII table with per-target rps/p50/p95/p99, a bottleneck
ranking (p95 desc), and (with ``--json PATH``) a machine summary.
Exit code is always 0 unless the probe itself fails; this measures,
it does not gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

from cloud_platform.api.v1.dependencies import get_token_authentication
from cloud_platform.planning.load_probe import (
    WorkerProbeConfig,
    probe_api,
    probe_worker_queue,
    rank_bottlenecks,
    render_report,
    summarize,
)
from cloud_platform.planning.probe_app import build_probe_app


async def run(requests: int, users: int, worker_jobs: int, worker_pool: int) -> list:
    """Measure every target and return the ProbeResult list."""
    app = build_probe_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe") as client:

        async def _get(url: str):  # type: ignore[no-untyped-def]
            async def _call() -> int:
                response = await client.get(url)
                return response.status_code

            return _call

        health_call = await _get("/health/live")
        offers_call = await _get("/v1/catalog/offers")
        servers_call = await _get("/v1/servers")
        keys_call = await _get("/v1/ssh-keys")

        results = []
        for name, call in (
            ("GET /health/live", health_call),
            ("GET /v1/catalog/offers", offers_call),
            ("GET /v1/servers", servers_call),
            ("GET /v1/ssh-keys", keys_call),
        ):
            results.append(
                await probe_api(
                    name,
                    call,
                    total_requests=requests,
                    concurrency=users,
                )
            )

    # Error-envelope path on a SECOND app instance WITHOUT the auth
    # override: missing credentials must fast-fail through the one error
    # envelope without touching any backing service.
    anon_app = build_probe_app()
    del anon_app.dependency_overrides[get_token_authentication]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=anon_app), base_url="http://probe"
    ) as anon_client:

        async def _unauth() -> int:
            response = await anon_client.get("/v1/wallet")
            return response.status_code

        results.append(
            await probe_api(
                "GET /v1/wallet unauth",
                _unauth,
                total_requests=requests,
                concurrency=users,
            )
        )

    from cloud_platform.worker.settings import reconcile_provider_resources

    results.append(
        await probe_worker_queue(
            WorkerProbeConfig(
                jobs=worker_jobs,
                workers=worker_pool,
                handler=reconcile_provider_resources,
            )
        )
    )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=200, help="requests per API target")
    parser.add_argument("--users", type=int, default=8, help="concurrent virtual users")
    parser.add_argument("--worker-jobs", type=int, default=500, help="jobs for the queue probe")
    parser.add_argument("--worker-pool", type=int, default=4, help="consumer tasks")
    parser.add_argument("--json", type=Path, default=None, help="write JSON summary here")
    args = parser.parse_args(argv)

    results = asyncio.run(
        run(
            requests=args.requests,
            users=args.users,
            worker_jobs=args.worker_jobs,
            worker_pool=args.worker_pool,
        )
    )
    report = render_report(results)
    print(report)
    bottleneck = rank_bottlenecks(results)[0]
    print(
        f"\nVerdict: '{bottleneck.name}' is the current bottleneck (p95={bottleneck.p95:.2f} ms)."
    )
    if args.json is not None:
        args.json.write_text(json.dumps(summarize(results), indent=2), encoding="utf-8")
        print(f"JSON summary written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
