"""add terms_versions table and users.terms_version column (M02-004)

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "terms_versions",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False, server_default=text("''")),
        sa.Column("effective_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
    )
    op.add_column("users", sa.Column("terms_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "terms_version")
    op.drop_table("terms_versions")
