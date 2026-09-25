"""Canonicalize only pristine legacy wallets to the storefront currency.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-25

Migration 0043 changed the creation default to USD but deliberately preserved
all existing wallet currency labels. That leaves an older, completely unused
zero-balance wallet unable to buy the now-canonical USD storefront offers.

This repair changes no money: only a zero-balance wallet with no ledger,
hold, server, or payment-session history is eligible. Any wallet carrying
financial or service history remains untouched and still requires an explicit
operator migration.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | None = None
depends_on: str | None = None

CANONICAL_WALLET_CURRENCY = "USD"


def _notify(message: str) -> None:
    print(f"[migration {revision}] {message}", flush=True)


def upgrade() -> None:
    bind = op.get_bind()

    wallets = sa.table(
        "wallets",
        sa.column("id"),
        sa.column("user_id"),
        sa.column("balance"),
        sa.column("currency"),
    )
    ledger = sa.table("ledger", sa.column("wallet_id"))
    holds = sa.table("holds", sa.column("wallet_id"))
    servers = sa.table("servers", sa.column("user_id"))
    payment_sessions = sa.table("payment_sessions", sa.column("user_id"))

    # A denomination may be changed automatically only when there is literally
    # no monetary or service fact whose meaning could change with it.
    no_ledger = ~sa.exists(
        sa.select(1).select_from(ledger).where(ledger.c.wallet_id == wallets.c.id)
    )
    no_holds = ~sa.exists(sa.select(1).select_from(holds).where(holds.c.wallet_id == wallets.c.id))
    no_servers = ~sa.exists(
        sa.select(1).select_from(servers).where(servers.c.user_id == wallets.c.user_id)
    )
    no_payments = ~sa.exists(
        sa.select(1)
        .select_from(payment_sessions)
        .where(payment_sessions.c.user_id == wallets.c.user_id)
    )

    statement = (
        sa.update(wallets)
        .where(
            wallets.c.balance == 0,
            wallets.c.currency != CANONICAL_WALLET_CURRENCY,
            no_ledger,
            no_holds,
            no_servers,
            no_payments,
        )
        .values(currency=CANONICAL_WALLET_CURRENCY)
    )
    result = bind.execute(statement)
    changed = getattr(result, "rowcount", None)
    suffix = str(changed) if isinstance(changed, int) and changed >= 0 else "unknown"
    _notify(f"canonicalized {suffix} pristine legacy wallet(s) to USD")


def downgrade() -> None:
    raise RuntimeError(
        "0044 has no automatic downgrade: the previous currency of a pristine "
        "wallet was intentionally not retained, and guessing it would relabel money."
    )
