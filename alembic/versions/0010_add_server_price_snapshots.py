"""add server_price_snapshots table (M06-002)

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "server_price_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column("server_id", postgresql.UUID(), nullable=False),
        sa.Column("provider_key", sa.String(), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("location_id", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("cost_minor", sa.BigInteger(), nullable=False),
        sa.Column("selling_minor", sa.BigInteger(), nullable=False),
        sa.Column("book_name", sa.String(), nullable=False),
        sa.Column("book_version", sa.Integer(), nullable=False),
        sa.Column("margin_rule", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("priced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("server_id", name="uq_server_price_snapshots_server_id"),
    )


def downgrade() -> None:
    op.drop_table("server_price_snapshots")
