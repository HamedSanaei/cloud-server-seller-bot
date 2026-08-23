"""add servers.contained_from (M10-007)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("contained_from", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("servers", "contained_from")
