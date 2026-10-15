"""Track configuration-managed Telegram Super Admin assignments additively.

Revision ID: 0055
Revises: 0054
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_superadmin_assignments",
        sa.Column("telegram_user_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.CheckConstraint("telegram_user_id > 0", name="ck_superadmin_telegram_user_id_positive"),
    )


def downgrade() -> None:
    # Authorization provenance is deliberately retained. A reviewed forward
    # migration is required to retire it; dropping it could orphan admin roles.
    pass
