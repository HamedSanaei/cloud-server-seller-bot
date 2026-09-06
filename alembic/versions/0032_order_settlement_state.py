"""Order payment settlement state (LEASEWEB-MVP)

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-06

Adds the durable local settlement sub-state to ``provider_orders``.
Provider acceptance and local charge settlement are DIFFERENT facts: the
provider order id is persisted first and never lost, but activation and
delivery are blocked until the wallet hold is CAPTURED with exactly one
CHARGE ledger entry. ``settlement_status`` (pending | complete |
needs_review), ``settlement_attempted_at``, ``settlement_attempts`` and
``settlement_error`` record that financial sub-state durably so an
accepted-but-unsettled order survives crashes and is repaired by the
reconciler (which retries the LOCAL capture only — never a provider POST).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "provider_orders",
        sa.Column("settlement_status", sa.String(32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "provider_orders",
        sa.Column("settlement_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_orders",
        sa.Column("settlement_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("provider_orders", sa.Column("settlement_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("provider_orders", "settlement_error")
    op.drop_column("provider_orders", "settlement_attempts")
    op.drop_column("provider_orders", "settlement_attempted_at")
    op.drop_column("provider_orders", "settlement_status")
