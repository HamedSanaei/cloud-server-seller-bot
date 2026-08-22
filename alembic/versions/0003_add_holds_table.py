"""add holds table for concurrent-safe wallet reservations

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "holds",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "wallet_id",
            sa.dialects.postgresql.UUID(),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="created",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("captured_at", sa.DateTime(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_holds_wallet_idempotency",
        "holds",
        ["wallet_id", "idempotency_key"],
    )
    op.create_index("ix_holds_wallet_id", "holds", ["wallet_id"])
    op.create_index("ix_holds_status", "holds", ["status"])


def downgrade() -> None:
    op.drop_index("ix_holds_status", table_name="holds")
    op.drop_index("ix_holds_wallet_id", table_name="holds")
    op.drop_constraint("uq_holds_wallet_idempotency", "holds", type_="unique")
    op.drop_table("holds")
