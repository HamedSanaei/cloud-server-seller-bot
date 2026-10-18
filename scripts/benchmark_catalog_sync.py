"""Reproducible real-PG catalog persistence benchmark. Never uses application DB settings.

Only the explicit loopback scratch URL accepted by the shared fixture is used.
Baseline is loaded from an archived source file, not a branch checkout/reset.
All records are generated; no provider network or business mutations are made.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.observability.catalog import install_sql_instrumentation, stage
from tests.live.catalog_performance_support import observation, scratch_catalog


def backend_cpu(pid: int) -> float | None:
    """Optional WSL PostgreSQL backend CPU; never claim container/host CPU."""
    try:
        raw = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "cat", f"/proc/{int(pid)}/stat"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        values = raw.rsplit(")", 1)[1].split()
        ticks = subprocess.run(
            ["wsl", "-d", "Ubuntu", "--", "getconf", "CLK_TCK"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        return (int(values[11]) + int(values[12])) / int(ticks)
    except (ValueError, IndexError, subprocess.SubprocessError, FileNotFoundError):
        return None


async def measure(
    repository_type: Any, *, batch: bool, scenario: str, count: int
) -> dict[str, Any]:
    async with scratch_catalog() as (engine, factory):
        install_sql_instrumentation(engine)
        candidate = SqlAlchemySellableOfferRepository(factory)
        initial = [observation(i) for i in range(count)]
        if scenario != "cold":
            for offset in range(0, count, 100):
                await candidate.upsert_provider_batch(initial[offset : offset + 100])
        items = (
            initial
            if scenario in {"cold", "warm"}
            else [observation(i, price=123) for i in range(count)]
        )
        if scenario == "partial_failure":
            items[count // 2] = dict(items[count // 2], provider_key="x" * 300)
        repo = repository_type(factory)
        async with engine.connect() as connection:
            pid = int(await connection.scalar(text("SELECT pg_backend_pid()")))
        pg_before = backend_cpu(pid)
        wall, cpu = time.perf_counter(), time.process_time()
        failures = 0
        with stage("benchmark") as sample:
            if batch:
                for offset in range(0, count, 100):
                    chunk = items[offset : offset + 100]
                    try:
                        await repo.upsert_provider_batch(chunk)
                    except Exception:
                        for item in chunk:
                            try:
                                await repo.upsert_from_provider(**item)
                            except Exception:
                                failures += 1
            else:
                for item in items:
                    try:
                        await repo.upsert_from_provider(**item)
                    except Exception:
                        failures += 1
        elapsed, cpu_used = time.perf_counter() - wall, time.process_time() - cpu
        pg_after = backend_cpu(pid)
        # Semantic equivalence is checked, not inferred from lower query counts.
        rows = await candidate.list_all()
        state = sorted(
            (
                r.product_id,
                r.provider_account_id,
                r.provider_cost_minor,
                r.selling_price_minor,
                r.enabled,
                r.operator_disabled,
                r.auto_priced,
            )
            for r in rows
        )
        return dict(
            wall_seconds=elapsed,
            worker_cpu_seconds=cpu_used,
            postgres_backend_cpu_seconds=(
                pg_after - pg_before if pg_after is not None and pg_before is not None else None
            ),
            sql_queries=sample.queries,
            commits=sample.commits,
            rollbacks=sample.rollbacks,
            failed_observations=failures,
            state=state,
        )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location(
        "catalog_baseline_repository", args.baseline_repository
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("baseline source unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = module.SqlAlchemySellableOfferRepository
    report: dict[str, Any] = dict(
        records=args.records,
        repetitions=args.repetitions,
        # Public Git commit identity, not a credential.
        baseline="f609f617e4223c85fec2073371c9dbfaf637a3ff",  # pragma: allowlist secret
        provider_network_calls=0,
        results={},
    )
    for scenario in ("cold", "warm", "changed", "partial_failure"):
        samples = {"baseline": [], "candidate": []}
        for repeat in range(args.repetitions):
            order = [
                ("baseline", baseline, False),
                ("candidate", SqlAlchemySellableOfferRepository, True),
            ]
            if repeat % 2:
                order.reverse()
            for label, cls, batch in order:
                samples[label].append(
                    await measure(cls, batch=batch, scenario=scenario, count=args.records)
                )
            assert samples["baseline"][-1]["state"] == samples["candidate"][-1]["state"], (
                "catalog output changed"
            )
        summary = {}
        for label, values in samples.items():
            for value in values:
                value.pop("state")
            summary[label] = {
                name: {
                    "median": statistics.median(v[name] for v in values if v[name] is not None),
                    "min": min(v[name] for v in values if v[name] is not None),
                    "max": max(v[name] for v in values if v[name] is not None),
                }
                for name in values[0]
                if any(v[name] is not None for v in values)
            }
        report["results"][scenario] = dict(summary=summary, samples=samples, semantic_parity=True)
        print(json.dumps({"scenario": scenario, "summary": summary}), flush=True)
        await asyncio.to_thread(
            Path(args.output).write_text, json.dumps(report, indent=2), encoding="utf-8"
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-repository", required=True)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output", default=".ci-artifacts/catalog-benchmark.json")
    args = parser.parse_args()
    if args.records < 1 or args.repetitions < 5:
        parser.error("positive records and at least five repetitions required")
    asyncio.run(run(args))
