"""Sync provider catalogs into the local database.

Populates locations, plans (offers) and images from every configured
provider (Hetzner, LeaseWeb, ArvanCloud) so the bot and REST v1 can sell
from the synced catalog.

Usage::

    uv run alembic upgrade head          # once, schema must exist
    uv run python scripts/sync_catalog.py
    uv run python scripts/sync_catalog.py --provider hetzner
    uv run python scripts/sync_catalog.py --provider leaseweb

Requires provider API keys and a reachable ``DATABASE_URL`` (both from
the environment or ``.env``). Upserts are idempotent: re-running only
inserts what changed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from cloud_platform.core.container import create_container


async def run(provider: str | None = None) -> int:
    container = create_container()
    try:
        jobs: list[tuple[str, object]] = []
        if (provider is None or provider == "hetzner") and container.hetzner_syncer is not None:
            jobs.append(("hetzner", container.hetzner_syncer))
        if (provider is None or provider == "leaseweb") and getattr(
            container, "leaseweb_syncer", None
        ) is not None:
            jobs.append(("leaseweb", container.leaseweb_syncer))
        if provider is None or provider == "arvancloud":
            for i, syncer in enumerate(container.arvancloud_syncers):
                jobs.append((f"arvancloud[{i}]", syncer))
        if provider in ("hetzner", "leaseweb") and not jobs:
            print(
                f"No API key configured for provider {provider!r}; "
                "set it in .env (HETZNER_API_TOKEN / LEASEWEB_API_KEY).",
                file=sys.stderr,
            )
            return 1
        if not jobs:
            print(
                "No provider API key is set; cannot sync any catalog. "
                "Set HETZNER_API_TOKEN and/or LEASEWEB_API_KEY.",
                file=sys.stderr,
            )
            return 1
        exit_code = 0
        for name, syncer in jobs:
            try:
                results = await syncer.sync_all()  # type: ignore[attr-defined]
            except Exception as exc:
                print(f"{name}: FAILED: {exc}", file=sys.stderr)
                exit_code = 1
                continue
            for step, result in results.items():
                print(
                    f"{name}/{step:<10} fetched={result.total_fetched} "
                    f"upserted={result.total_upserted} skipped={result.total_skipped}"
                )
                for err in result.errors:
                    print(f"{name}/{step}: {err}", file=sys.stderr)
        return exit_code
    finally:
        if container.hetzner_syncer is not None:
            await container.hetzner_syncer.close()
        await container.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("hetzner", "leaseweb", "arvancloud"),
        default=None,
        help="Sync only this provider (default: all configured).",
    )
    args = parser.parse_args(argv)
    return asyncio.run(run(args.provider))


if __name__ == "__main__":
    raise SystemExit(main())
