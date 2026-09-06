"""One-shot local development bring-up (M08).

Starts the compose stack, applies the schema, and syncs the Hetzner catalog
so the selling bot can run locally:

1. ``docker compose up -d postgres redis``   (Docker Desktop must be running)
2. ``uv run alembic upgrade head``
3. Hetzner catalog sync (locations / plans / images)

Usage::

    uv run python scripts/setup_dev.py

The sync step needs ``HETZNER_API_TOKEN`` (from ``.env`` or the
environment). If the token is missing the step is skipped with a warning and
the script still exits 0, so the stack + schema alone can be brought up;
pass ``--skip-sync`` to skip it explicitly.

Environment: reads ``.env`` if present (via pydantic-settings). Without
``.env`` the defaults already match docker-compose.yml
(``cloud:cloud@localhost:5432/cloud``).
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys

import asyncpg

from cloud_platform.core.config import get_settings
from cloud_platform.core.container import create_container


def _run(command: list[str]) -> int:
    """Run a subprocess, streaming its output, and return the exit code."""
    print("$ " + " ".join(command), flush=True)
    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 127
    return completed.returncode


async def _wait_for_postgres(url: str, max_wait: float = 120.0) -> bool:
    """Poll the database until it accepts connections (bounded wait)."""
    # SQLAlchemy DSN -> asyncpg DSN
    dsn = url.replace("+asyncpg", "", 1)
    deadline = asyncio.get_running_loop().time() + max_wait
    attempt = 0
    while True:
        try:
            conn = await asyncio.wait_for(asyncpg.connect(dsn, timeout=5.0), timeout=6.0)
            await conn.close()
            return True
        except (OSError, asyncpg.PostgresError, TimeoutError):
            attempt += 1
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            print(
                f"  waiting for postgres (attempt {attempt}, {remaining:.0f}s left)...",
                flush=True,
            )
            await asyncio.sleep(2.0)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()

    # 1. Compose stack -----------------------------------------------------
    print("==> Starting postgres + redis (docker compose)")
    if _run(["docker", "compose", "up", "-d", "postgres", "redis"]) != 0:
        print("ERROR: docker compose failed. Is Docker Desktop running?", file=sys.stderr)
        return 1

    if not await _wait_for_postgres(settings.database_url):
        print(
            "ERROR: postgres did not become ready in time. Check `docker compose logs postgres`.",
            file=sys.stderr,
        )
        return 1
    print("  postgres is ready", flush=True)

    # 2. Schema ------------------------------------------------------------
    print("==> Applying schema (alembic upgrade head)")
    if _run([sys.executable, "-m", "alembic", "upgrade", "head"]) != 0:
        print("ERROR: alembic upgrade failed", file=sys.stderr)
        return 1

    # 3. Hetzner catalog sync ----------------------------------------------
    if args.skip_sync:
        print("==> Skipping catalog sync (--skip-sync)")
        return 0
    if not settings.hetzner_api_token:
        print(
            "==> Skipping catalog sync: HETZNER_API_TOKEN is not set "
            "(add it to .env, then re-run scripts/sync_catalog.py).",
            file=sys.stderr,
        )
        return 0

    print("==> Syncing Hetzner catalog")
    container = create_container()
    syncer = container.hetzner_syncer
    assert syncer is not None  # token is set, so the adapter is registered
    try:
        results = await syncer.sync_all()
    finally:
        await syncer.close()
        await container.close()
    for name, result in results.items():
        print(
            f"  {name:<10} fetched={result.total_fetched} "
            f"upserted={result.total_upserted} skipped={result.total_skipped}"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local dev bring-up: compose stack + schema + Hetzner catalog sync"
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Skip the Hetzner catalog sync step",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
