"""add servers.last_accrued_at and accrual_periods table (M06-005)

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("last_accrued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "accrual_periods",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(),
            sa.ForeignKey("servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quanta", sa.Integer(), nullable=False, server_default=text("1")),
        sa.Column("cost_minor", sa.BigInteger(), nullable=False, server_default=text("0")),
        sa.Column("selling_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("idempotency_key", sa.String(), unique=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("accrual_periods")
    op.drop_column("servers", "last_accrued_at")
