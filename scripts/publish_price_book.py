"""Publish a price-book version with explicit margin rules (ops tooling).

The selling price of every offer derives from a versioned price book
(M06-001); without at least one active version the buy confirmation and
the create command fail cleanly. This script is the operator path to
publish the first version (and later margin updates):

    uv run python scripts/publish_price_book.py \\
        --reason "launch margins: 20% on everything" \\
        --admin-telegram-id 123456789 --bootstrap-admin

    uv run python scripts/publish_price_book.py \\
        --rules-file margins.json --reason "hetzner DE cheaper" \\
        --admin-telegram-id 123456789

Rules file format (JSON list; margin_factor is a decimal string)::

    [
      {"provider": "hetzner", "plan": "*", "location": "*",
       "margin_factor": "1.20", "fixed_minor": 0},
      {"provider": "leaseweb", "plan": "*", "location": "*",
       "margin_factor": "1.25", "fixed_minor": 0, "monthly_cap_minor": 500000}
    ]

Without ``--rules-file`` a single wildcard rule ``(*, *, *)`` is published
from ``--margin-factor`` / ``--fixed-minor``. Money is Decimal-only; the
version number is assigned as max+1 and concurrent publishes fail loudly
(unique constraint) instead of clobbering each other. Every publish is
audited (pricing.publish_book_version) with a non-empty reason.

Admin resolution: the actor must be an ADMIN user. ``--bootstrap-admin``
creates (or promotes) the operator row for the given Telegram id, so the
very first publish on a fresh database works in one command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from cloud_platform.core.config import get_settings
from cloud_platform.core.container import create_container
from cloud_platform.db.base import User as UserRow
from cloud_platform.modules.pricing.domain import MarginRule
from cloud_platform.modules.pricing.repository import SqlAlchemyPriceBookRepository
from cloud_platform.modules.pricing.service import PriceBookService
from cloud_platform.modules.users.domain import Role, User
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository


def _parse_rules_file(path: str) -> tuple[MarginRule, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("rules file must be a non-empty JSON list")
    rules: list[MarginRule] = []
    for row in raw:
        rules.append(
            MarginRule(
                provider=str(row["provider"]),
                plan=str(row.get("plan", "*")),
                location=str(row.get("location", "*")),
                margin_factor=Decimal(str(row["margin_factor"])),
                fixed_minor=int(row.get("fixed_minor", 0)),
                monthly_cap_minor=(
                    None if row.get("monthly_cap_minor") is None else int(row["monthly_cap_minor"])
                ),
            )
        )
    return tuple(rules)


async def _resolve_admin(container: object, telegram_id: int, bootstrap: bool) -> User:
    """Return the ADMIN user for ``telegram_id`` (bootstrap it if asked)."""
    users = SqlAlchemyUserRepository(container.session_factory)  # type: ignore[attr-defined]
    admin = await users.get_by_telegram_user_id(telegram_id)
    if admin is not None:
        if admin.role is not Role.ADMIN:
            if not bootstrap:
                raise PermissionError(
                    f"user {telegram_id} exists but is not an admin "
                    f"(role={admin.role.value}); re-run with --bootstrap-admin to promote"
                )
            async with container.session_factory() as session:  # type: ignore[attr-defined]
                row = (
                    (await session.execute(select(UserRow).where(UserRow.id == admin.id)))
                    .scalars()
                    .one()
                )
                row.role = Role.ADMIN.value
                await session.commit()
            print(f"promoted telegram user {telegram_id} to admin")
            admin = await users.get_by_telegram_user_id(telegram_id)
            assert admin is not None
        return admin
    if not bootstrap:
        raise LookupError(
            f"no user with telegram id {telegram_id}; re-run with --bootstrap-admin "
            "to create the first admin operator"
        )
    async with container.session_factory() as session:  # type: ignore[attr-defined]
        session.add(
            UserRow(
                username=f"admin-{telegram_id}",
                email=f"admin-{telegram_id}@telegram.local",
                status="active",
                role=Role.ADMIN.value,
                telegram_user_id=telegram_id,
            )
        )
        await session.commit()
    print(f"created admin operator for telegram user {telegram_id}")
    admin = await users.get_by_telegram_user_id(telegram_id)
    assert admin is not None
    return admin


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    book_name = args.book or settings.price_book_name
    if args.rules_file:
        rules = _parse_rules_file(args.rules_file)
    else:
        rules = (
            MarginRule(
                provider="*",
                plan="*",
                location="*",
                margin_factor=Decimal(args.margin_factor),
                fixed_minor=args.fixed_minor,
            ),
        )
    effective_at = (
        datetime.fromisoformat(args.effective_at) if args.effective_at else datetime.now(UTC)
    )
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)

    container = create_container()
    try:
        admin = await _resolve_admin(container, args.admin_telegram_id, args.bootstrap_admin)
        service = PriceBookService(
            SqlAlchemyPriceBookRepository(container.session_factory),
            container.audit_repository(),
        )
        created = await service.publish_version(
            book_name=book_name,
            rules=rules,
            effective_at=effective_at,
            actor=admin,
            reason=args.reason,
        )
        print(f"published {created.book_name} v{created.version} ({len(rules)} rules)")
        return 0
    finally:
        await container.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", default=None, help="Price book name (default: PRICE_BOOK_NAME)")
    parser.add_argument("--rules-file", default=None, help="JSON margin-rules file")
    parser.add_argument("--margin-factor", default="1.20", help="Wildcard margin when no file")
    parser.add_argument("--fixed-minor", type=int, default=0, help="Flat addition in minor units")
    parser.add_argument("--reason", required=True, help="Non-empty audit reason (required)")
    parser.add_argument("--effective-at", default=None, help="ISO instant (default: now)")
    parser.add_argument("--admin-telegram-id", type=int, required=True)
    parser.add_argument("--bootstrap-admin", action="store_true")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
