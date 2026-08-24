"""add operations.traceparent column (M11-002)

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ADD COLUMN (nullable) - rolling-deploy safe: old code ignores it, new
    # code treats NULL as "no parent span" (root span).
    op.add_column("operations", sa.Column("traceparent", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("operations", sa.Column("traceparent", sa.String()))
