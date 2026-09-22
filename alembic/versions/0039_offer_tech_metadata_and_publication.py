"""Offer technical metadata and operator publication intent (STOREFRONT-V2).

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-23

WHAT THIS REVISION DOES
------------------------
Adds three columns to ``sellable_offers`` (all inspector-guarded, so a
re-run is a no-op):

* ``technical_metadata`` (JSONB, default ``{}``) — provider-neutral,
  customer-visible technical facts normalized by the provider adapters
  (architecture, storage type, ...). Secrets, credential ids and raw API
  payloads must never be stored here.
* ``operator_disabled`` (boolean, default FALSE) — the explicit operator
  block. Automatic publishing may enable an offer only while this is FALSE;
  catalog syncs never write it.
* ``auto_priced`` (boolean, default TRUE) — whether the automatic pricing
  policy owns the selling price. A manual ``offers price`` command clears it.

DATA BACKFILL (forward-only, derived from stored state — never guessed)
------------------------------------------------------------------------
* Rows currently ``enabled = FALSE`` become ``operator_disabled = TRUE``:
  before this revision the only way to hide an offer was ``enabled`` itself,
  so every disabled row represents explicit operator intent — including the
  intentionally disabled production offer, which stays disabled.
* Rows that already carry a selling price (``selling_price_minor > 0``)
  become ``auto_priced = FALSE``: before this revision the only way to price
  an offer was the manual CLI, so every existing price is operator-owned and
  the automatic policy must not overwrite it.

WHAT IT DELIBERATELY DOES NOT DO
---------------------------------
* No column is dropped or renamed; no history is rewritten.
* ``enabled`` keeps its meaning (the sale gate); ``operator_disabled`` is the
  durable intent behind it.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "sellable_offers"


def _notify(message: str) -> None:
    """Make the revision visible in the deployment log."""
    print(f"[migration {revision}] {message}", flush=True)


def _column_names(table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _column_names(TABLE)
    added: list[str] = []
    if "technical_metadata" not in existing:
        op.add_column(
            TABLE,
            sa.Column("technical_metadata", JSONB, nullable=False, server_default="{}"),
        )
        added.append("technical_metadata")
    if "operator_disabled" not in existing:
        op.add_column(
            TABLE,
            sa.Column("operator_disabled", sa.Boolean(), nullable=False, server_default="false"),
        )
        added.append("operator_disabled")
    if "auto_priced" not in existing:
        op.add_column(
            TABLE,
            sa.Column("auto_priced", sa.Boolean(), nullable=False, server_default="true"),
        )
        added.append("auto_priced")
    if not added:
        _notify("sellable_offers already carries the publication columns — nothing to do")
        return
    _notify(f"added {', '.join(added)} to sellable_offers")

    offers = sa.table(
        TABLE,
        sa.column("enabled", sa.Boolean),
        sa.column("selling_price_minor", sa.BigInteger),
        sa.column("operator_disabled", sa.Boolean),
        sa.column("auto_priced", sa.Boolean),
    )
    bind = op.get_bind()
    disabled = int(
        bind.execute(
            sa.update(offers).where(offers.c.enabled.is_(False)).values(operator_disabled=True)
        ).rowcount
        or 0
    )
    manual = int(
        bind.execute(
            sa.update(offers).where(offers.c.selling_price_minor > 0).values(auto_priced=False)
        ).rowcount
        or 0
    )
    _notify(
        f"preserved operator intent: {disabled} disabled row(s) marked "
        f"operator_disabled, {manual} priced row(s) marked manual (auto_priced=false)"
    )


def downgrade() -> None:
    """Refuse: this revision backfills operator intent that cannot be rebuilt."""
    raise RuntimeError(
        "0039 has no automatic downgrade: dropping the publication columns would "
        "destroy operator intent (which offers were explicitly disabled or "
        "manually priced). Restore the database from a backup instead."
    )
