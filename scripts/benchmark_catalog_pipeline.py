"""Real-PostgreSQL coordinator pricing/publication benchmark, no provider network.

Loads repository AND coordinator from a git archive, never imports private config.
Only explicitly opted-in loopback scratch schemas; synthetic provider observations.
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
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import update

from cloud_platform.db.base import SellableOffer as Row
from cloud_platform.modules.catalog.repository import PostgresAdvisoryCatalogSyncLock
from cloud_platform.modules.offers.auto_sync import CatalogAutoSyncCoordinator
from cloud_platform.modules.offers.batch import ObservationBuffer
from cloud_platform.modules.offers.domain import (
    CatalogSyncReport,
    PricingPolicy,
    has_valid_pricing_provenance,
)
from cloud_platform.modules.offers.pricing import CatalogOfferPricer
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
from cloud_platform.observability.catalog import install_sql_instrumentation, stage
from tests.live.catalog_performance_support import observation, scratch_catalog


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("archived module required")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def prices(repo, offers):
    requests = []
    for offer in offers:
        price = await CatalogOfferPricer(None, "USD").price_auto(
            offer, PricingPolicy(markup_percent=10)
        )
        requests.append(
            dict(
                offer_id=offer.id,
                expected_cost_minor=offer.provider_cost_minor,
                expected_cost_currency="USD",
                expected_provider_rate=offer.billing_parameters["provider_hourly_rate"],
                selling_price_minor=price.selling_price_minor,
                selling_currency="USD",
                pricing_metadata=dict(price.pricing_metadata),
            )
        )
    for offset in range(0, len(requests), 100):
        await repo.set_auto_prices_batch_if_current(requests[offset : offset + 100])


async def measure(repo_type, coordinator_type, *, batch, scenario, count):
    async with scratch_catalog(pool_size=2) as (engine, factory):
        install_sql_instrumentation(engine)
        initial = [observation(i) for i in range(count)]
        reader = SqlAlchemySellableOfferRepository(factory)
        if scenario != "cold":
            seeded = []
            for offset in range(0, count, 100):
                seeded.extend(await reader.upsert_provider_batch(initial[offset : offset + 100]))
            await prices(reader, seeded)
            async with factory.begin() as session:
                await session.execute(update(Row).values(enabled=True))
        items = (
            initial
            if scenario in {"cold", "warm"}
            else [observation(i, price=123) for i in range(count)]
        )
        if scenario == "partial_failure":
            items[count // 2] = dict(items[count // 2], provider_key="x" * 300)
        repository = repo_type(factory)

        async def sync():
            buffer = ObservationBuffer(repository, enabled=batch)
            failures, written = [], 0
            for item in items:
                try:
                    await buffer.upsert_from_provider(**item)
                    written += 1
                except Exception as error:
                    failures.append(type(error).__name__)
            if buffer.enabled:
                written, errors = await buffer.flush()
                failures.extend(errors)
            verified = (
                frozenset(
                    (i["provider_account_id"], i["product_id"], i["location_id"]) for i in items
                )
                if not failures
                else frozenset()
            )
            return CatalogSyncReport(
                provider_key="leaseweb",
                billing_model="hourly",
                account_aware=True,
                ok=not failures,
                complete=not failures,
                discovered=count,
                persisted=written,
                persistence_failures=tuple(failures),
                verified_accounts=verified,
            )

        options = dict(
            sources=[SimpleNamespace(provider_key="leaseweb", sync_catalog=sync)],
            offers=repository,
            state=AsyncMock(),
            lock=PostgresAdvisoryCatalogSyncLock(
                factory, key=int.from_bytes(uuid4().bytes[:8], "big", signed=True)
            ),
            pricing_policies={"leaseweb.hourly": PricingPolicy(markup_percent=10)},
            catalog_min_fresh_lifetime_seconds=1200,
        )
        if batch:
            options["batch_enabled"] = True
        coordinator = coordinator_type(**options)
        wall, cpu = time.perf_counter(), time.process_time()
        with stage("pipeline_benchmark") as sample:
            report = await coordinator.run()
        elapsed, cpu_used = time.perf_counter() - wall, time.process_time() - cpu
        assert report.ran and report.providers[0].ok == (scenario != "partial_failure")
        # Audit evidence timestamps differ by run; compare ALL economic/spec
        # facts, exact native rates and actual fail-closed sellability instead.
        rows = await reader.list_all()
        state = sorted(
            (
                row.provider_key,
                row.product_id,
                row.location_id,
                row.billing_model,
                row.provider_account_id,
                row.provider_cost_currency,
                row.provider_cost_minor,
                row.selling_currency,
                row.selling_price_minor,
                row.provider_available,
                row.enabled,
                row.operator_disabled,
                row.auto_priced,
                json.dumps(row.billing_parameters, sort_keys=True),
                json.dumps(row.technical_metadata, sort_keys=True),
                row.name,
                row.vcpu,
                row.ram_gb,
                row.disk_gb,
                row.traffic,
                row.sellable and has_valid_pricing_provenance(row, "USD"),
            )
            for row in rows
        )
        return dict(
            wall_seconds=elapsed,
            worker_cpu_seconds=cpu_used,
            sql_queries=sample.queries,
            commits=sample.commits,
            rollbacks=sample.rollbacks,
            state=state,
        )


async def run(args):
    archived = Path(args.baseline_root) / "src/cloud_platform/modules/offers"
    baseline_repo = load(
        "pipeline_baseline_repo", archived / "repository.py"
    ).SqlAlchemySellableOfferRepository
    baseline_coordinator = load(
        "pipeline_baseline_coordinator", archived / "auto_sync.py"
    ).CatalogAutoSyncCoordinator
    report = dict(
        records=args.records,
        repetitions=5,
        scope=(
            "coordinator + PostgreSQL lock + observation + pricing + publication; "
            "provider/state-repository stubbed"
        ),
        provider_network_calls=0,
        results={},
    )
    for scenario in ("cold", "warm", "changed", "partial_failure"):
        samples = {"baseline": [], "candidate": []}
        for repeat in range(5):
            order = [
                ("baseline", baseline_repo, baseline_coordinator, False),
                ("candidate", SqlAlchemySellableOfferRepository, CatalogAutoSyncCoordinator, True),
            ]
            if repeat % 2:
                order.reverse()
            pair = {}
            for name, repo, coordinator, batch in order:
                pair[name] = await measure(
                    repo, coordinator, batch=batch, scenario=scenario, count=args.records
                )
            assert pair["baseline"]["state"] == pair["candidate"]["state"], (
                "financial/catalog output changed"
            )
            for name, value in pair.items():
                value.pop("state")
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
        report["results"][scenario] = dict(samples=samples, summary=summary, financial_parity=True)
        print(json.dumps(dict(scenario=scenario, summary=summary)), flush=True)
        await asyncio.to_thread(
            Path(args.output).write_text, json.dumps(report, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", required=True)
    parser.add_argument("--records", type=int, default=1000)
    parser.add_argument("--output", default=".ci-artifacts/catalog-pipeline-benchmark.json")
    args = parser.parse_args()
    if args.records < 1:
        parser.error("positive record count required")
    asyncio.run(run(args))
