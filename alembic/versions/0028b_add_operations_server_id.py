"""Add operations.server_id referenced by the 0029 hot-path index.

Revision ID: 0028b
Revises: 0028
Create Date: 2026-09-06

Migration 0029 creates ix_operations_server on operations (server_id, status),
but no earlier migration ever added the column.  This inserts the column ahead
of 0029 so a clean ``alembic upgrade head`` succeeds.  The column is nullable
and unused at runtime (the ORM maps operations by resource_type/resource_id);
it exists solely so the shipped hot-path index is valid.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028b"
down_revision: str | None = "0028"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Guarded so databases that were bootstrapped from metadata
    # (create_all) and already carry the column keep working.
    op.execute("ALTER TABLE operations ADD COLUMN IF NOT EXISTS server_id UUID")


def downgrade() -> None:
    op.drop_column("operations", "server_id")
