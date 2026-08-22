"""add telegram_user_id to users

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
    )
    op.create_unique_constraint("uq_users_telegram_user_id", "users", ["telegram_user_id"])
    op.create_index("ix_users_telegram_user_id", "users", ["telegram_user_id"])


def downgrade() -> None:
    op.drop_index("ix_users_telegram_user_id", table_name="users")
    op.drop_constraint("uq_users_telegram_user_id", "users", type_="unique")
    op.drop_column("users", "telegram_user_id")
