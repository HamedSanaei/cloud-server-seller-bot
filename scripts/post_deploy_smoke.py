"""Post-deploy smoke test (M12-007).

Acceptance: **health + safe read-only provider checks.**

Run after `docker compose up -d` (production runbook, step 4). It is
deliberately read-only: it never mutates anything, never creates a server,
and the provider check is a token-scoped read (list images) that proves the
credential and network path are alive.

Usage:
    uv run python scripts/post_deploy_smoke.py --base-url http://127.0.0.1:8000 \
        --expected-head 0020
    # provider check (optional; needs the provider token in the env)
    uv run python scripts/post_deploy_smoke.py --base-url ... --expected-head 0020 \
        --provider-key hetzner

Exit code 0 = every check passed; non-zero = the deploy should not be
considered healthy (see the production runbook rollback section).
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from cloud_platform.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    """A plain GET (stdlib only - the smoke test must run anywhere)."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except Exception as exc:  # urllib wraps HTTPError + URLError + timeouts
        return 0, type(exc).__name__


def check_health(base_url: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    for path in ("/health/live", "/health/ready"):
        status, body = http_get(f"{base_url}{path}")
        ok = status == 200
        try:
            payload = json.loads(body) if ok else {}
        except json.JSONDecodeError:
            payload = {}
        ok = ok and isinstance(payload, dict) and payload.get("status") == "ok"
        results.append(
            CheckResult(
                f"GET {path}",
                ok,
                f"status={status}, expected status marker={ok}",
            )
        )
    return results


def check_metrics(base_url: str) -> CheckResult:
    status, body = http_get(f"{base_url}/metrics")
    ok = status == 200 and "cloud_platform_" in body
    return CheckResult(
        "GET /metrics",
        ok,
        f"status={status}, platform metrics present={ok}",
    )


def repo_head_revision() -> str:
    """The alembic head in the current checkout (what a deploy should reach).

    A head is a revision that no other revision's down_revision references.
    """
    revisions: set[str] = set()
    referenced: set[str] = set()
    for path in (REPO_ROOT / "alembic" / "versions").glob("*.py"):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name, value = node.targets[0].id, node.value
            else:
                continue
            if name not in {"revision", "down_revision"} or value is None:
                continue
            literal = ast.literal_eval(value)
            if name == "revision":
                if not isinstance(literal, str) or literal in revisions:
                    raise ValueError(f"invalid or duplicate migration revision in {path.name}")
                revisions.add(literal)
            elif literal is not None:
                referenced.update((literal,) if isinstance(literal, str) else literal)
    heads = sorted(revisions - referenced)
    return heads[0] if len(heads) == 1 else (",".join(heads) if heads else "")


def database_revisions() -> str:
    """Observe the configured database, never infer its state from the checkout."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from cloud_platform.core.config import get_settings

    async def read() -> str:
        engine = create_async_engine(get_settings().database_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                return ",".join(sorted(result.scalars().all()))
        finally:
            await engine.dispose()

    return asyncio.run(read())


def check_migration_head(base_url: str, expected_head: str | None) -> CheckResult:
    """The DEPLOYED database's head vs the release's head.

    Run this script with the deployed application's configuration. An HTTP
    liveness response or a locally expected revision is not database evidence.
    """
    if expected_head is None:
        expected_head = repo_head_revision()
    try:
        observed = database_revisions()
    except Exception as exc:
        return CheckResult(
            "alembic current == head", False, f"database probe: {type(exc).__name__}"
        )
    ok = bool(expected_head) and "," not in expected_head and observed == expected_head
    return CheckResult(
        "alembic current == head", ok, f"expected={expected_head}, observed={observed}"
    )


def worker_health() -> int:
    """ARQ's expiring Redis health sentinel proves the worker actually ran."""
    from arq.worker import check_health

    from cloud_platform.worker.settings import WorkerSettings

    return int(check_health(WorkerSettings))


def check_worker(base_url: str) -> CheckResult:
    """An API returning HTTP 200 does not establish worker health."""
    try:
        ok = worker_health() == 0
    except Exception as exc:
        return CheckResult("worker/ARQ sentinel", False, f"worker probe: {type(exc).__name__}")
    return CheckResult("worker/ARQ sentinel", ok, f"live Redis health sentinel={ok}")


async def _financial_snapshot(session: AsyncSession) -> tuple[int, int, int, int]:
    """Reuse the domain reconciler; never duplicate denomination/hold rules."""
    from sqlalchemy import func, select

    from cloud_platform.db.base import PaymentSession
    from cloud_platform.modules.wallet.reconciliation import LedgerReconciliationService
    from cloud_platform.modules.wallet.repository import (
        SqlAlchemyHoldRepository,
        SqlAlchemyLedgerRepository,
        SqlAlchemyWalletRepository,
    )

    @asynccontextmanager
    async def shared_session() -> AsyncIterator[AsyncSession]:
        # Repositories enter/exit this context repeatedly. Only the caller
        # owns the session/transaction: exiting a repository cannot close it.
        yield session

    report = await LedgerReconciliationService(
        SqlAlchemyWalletRepository(shared_session),
        SqlAlchemyHoldRepository(shared_session),
        SqlAlchemyLedgerRepository(shared_session),
    ).generate_report()
    payments = await session.execute(select(func.count()).select_from(PaymentSession))
    return report.wallets_checked, len(report.errors), len(report.warnings), payments.scalar_one()


async def financial_readiness() -> tuple[int, int, int, int]:
    """One consistent read-only snapshot, not racing separate wallet reads."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from cloud_platform.core.config import get_settings

    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                return await _financial_snapshot(session)
    finally:
        await engine.dispose()


def check_financial_read_only() -> CheckResult:
    """No payment creation/inquiry, wallet write or customer data in output."""
    try:
        wallets, errors, warnings, payments = asyncio.run(financial_readiness())
    except Exception as exc:
        return CheckResult(
            "payment/wallet read-only snapshot", False, f"financial probe: {type(exc).__name__}"
        )
    return CheckResult(
        "payment/wallet read-only snapshot",
        errors == 0,
        f"wallets={wallets}, ledger errors={errors}, warnings={warnings}, "
        f"payment sessions={payments}",
    )


def check_provider_read_only(
    provider_key: str, base_url_env: str | None, settings: Settings
) -> CheckResult:
    """Read each managed token's Project catalog and complete inventory only."""
    import asyncio

    from cloud_platform.providers.hetzner.accounts import build_hetzner_account_router

    async def _read() -> CheckResult:
        configured = settings
        if base_url_env:
            configured = settings.model_copy(update={"hetzner_api_base_url": base_url_env})
        router = build_hetzner_account_router(configured)
        if router is None or not router.providers:
            if router is not None:
                await router.aclose()
            return CheckResult(
                f"provider {provider_key} read",
                True,
                "skipped: no managed provider credential accounts configured",
            )
        details: list[str] = []
        ok = True
        try:
            for account_id, provider in router.providers.items():
                state = router.account_states[account_id].value
                try:
                    images = await provider.list_images()
                    usage = await router.server_usage(account_id)
                except Exception as exc:
                    ok = False
                    details.append(
                        f"account {account_id} ({state}): FAILED ({type(exc).__name__}); "
                        "Project availability unknown"
                    )
                    continue
                ceiling = str(usage.server_limit) if usage.server_limit is not None else "unknown"
                details.append(
                    f"account {account_id} ({state}): {len(images)} images; "
                    f"Project servers={usage.server_count}; "
                    f"operator-configured server ceiling={ceiling}"
                )
        finally:
            await router.aclose()
        return CheckResult(
            f"provider {provider_key} read (Project inventory)", ok, "; ".join(details)
        )

    try:
        return asyncio.run(_read())
    except Exception as exc:
        return CheckResult(
            f"provider {provider_key} read",
            False,
            f"FAILED: {type(exc).__name__}",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="post_deploy_smoke")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected-head", default=None, help="override the expected alembic head")
    parser.add_argument("--provider-key", default=None, choices=[None, "hetzner"])
    parser.add_argument("--provider-base-url", default=None)
    args = parser.parse_args(argv)

    results: list[CheckResult] = []
    results.extend(check_health(args.base_url))
    results.append(check_metrics(args.base_url))
    results.append(check_migration_head(args.base_url, args.expected_head))
    results.append(check_worker(args.base_url))
    results.append(check_financial_read_only())
    if args.provider_key:
        from cloud_platform.core.config import get_settings

        settings = get_settings()
        results.append(
            check_provider_read_only(args.provider_key, args.provider_base_url, settings)
        )

    all_ok = True
    for result in results:
        marker = "ok  " if result.ok else "FAIL"
        print(f"[{marker}] {result.name}: {result.detail}")
        all_ok = all_ok and result.ok

    print("smoke test " + ("PASSED" if all_ok else "FAILED - do not consider the deploy healthy"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
