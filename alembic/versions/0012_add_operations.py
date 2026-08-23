"""add operations table (M07-002)

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "operations",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column("operation_key", sa.String(), nullable=False),
        sa.Column("operation_type", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", postgresql.UUID(), nullable=False),
        sa.Column("provider_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=text("'pending'")),
        sa.Column("provider_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("operation_key", name="uq_operations_operation_key"),
    )
    op.create_index("ix_operations_status", "operations", ["status"])
    op.create_index("ix_operations_resource", "operations", ["resource_type", "resource_id"])


def downgrade() -> None:
    op.drop_index("ix_operations_resource", table_name="operations")
    op.drop_index("ix_operations_status", table_name="operations")
    op.drop_table("operations")
