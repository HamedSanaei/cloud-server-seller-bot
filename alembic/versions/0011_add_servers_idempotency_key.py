"""add idempotency_key to servers (M07-001)

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("idempotency_key", sa.String(), nullable=True))
    op.create_index(
        "uq_servers_idempotency_key",
        "servers",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_servers_idempotency_key", table_name="servers")
    op.drop_column("servers", "idempotency_key")
