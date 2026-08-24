"""add maintenance_blocks table (M10-005)

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_blocks",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column("provider_key", sa.String(), nullable=False),
        sa.Column("location_id", sa.String(), nullable=False, server_default=text("''")),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("provider_key", "location_id", name="uq_maintenance_blocks_scope"),
    )


def downgrade() -> None:
    op.drop_table("maintenance_blocks")
