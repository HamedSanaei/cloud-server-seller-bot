"""Persist one audited wallet denomination change without rewriting old ledger facts.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "wallet_currency_migrations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("wallet_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("source_balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("operator_id", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("close_entry_id", sa.UUID(), nullable=True),
        sa.Column("open_entry_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["close_entry_id"], ["ledger.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["open_entry_id"], ["ledger.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("source_balance_minor >= 0", name="ck_wallet_migration_nonnegative"),
    )


def downgrade() -> None:
    raise RuntimeError("0052 cannot be downgraded: currency migration audit is immutable")
