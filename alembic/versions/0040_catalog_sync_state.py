"""Per-provider automatic-sync status (STOREFRONT-V2).

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-23

WHAT THIS REVISION DOES
------------------------
Creates ``catalog_sync_state``: exactly ONE row per provider key, upserted by
the automatic sync coordinator after every run. It records the last attempt,
the last success, the last run's counters (discovered / persisted / prices
updated / published / retired) and the last run's warnings and errors, so the
operator diagnostics (``catalog auto-sync doctor``) can show them without
reading logs.

New table only — no existing data is touched, no backfill is needed, and a
re-run is a no-op when the table already exists.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "catalog_sync_state"


def _notify(message: str) -> None:
    """Make the revision visible in the deployment log."""
    print(f"[migration {revision}] {message}", flush=True)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE in set(inspector.get_table_names()):
        _notify("catalog_sync_state already exists — nothing to do")
        return
    op.create_table(
        TABLE,
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("persisted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prices_updated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("published", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retired", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warnings", JSONB, nullable=False, server_default="[]"),
        sa.Column("errors", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("provider_key"),
    )
    _notify("created catalog_sync_state (one row per provider, coordinator-owned)")


def downgrade() -> None:
    """Refuse: operator diagnostics history must not be dropped silently."""
    raise RuntimeError(
        "0040 has no automatic downgrade: dropping catalog_sync_state would "
        "destroy sync history. Restore the database from a backup instead."
    )
