"""add abuse_cases table (M10-006)

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "abuse_cases",
        sa.Column(
            "id",
            PG_UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column("provider_key", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("user_id", PG_UUID(), nullable=False),
        sa.Column("server_id", PG_UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reporter", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=text("'open'")),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_abuse_cases_user", "abuse_cases", ["user_id"])
    op.create_index("ix_abuse_cases_status", "abuse_cases", ["status"])


def downgrade() -> None:
    op.drop_index("ix_abuse_cases_status", table_name="abuse_cases")
    op.drop_index("ix_abuse_cases_user", table_name="abuse_cases")
    op.drop_table("abuse_cases")
