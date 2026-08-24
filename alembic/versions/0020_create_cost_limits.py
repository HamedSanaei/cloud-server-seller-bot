"""create cost_limits table (M10-004)

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "cost_limits",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuid_generate_v4()"),
            primary_key=True,
        ),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column(
            "provider_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_accounts.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("limit_minor", sa.BigInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=True,
        ),
        sa.UniqueConstraint("scope", "provider_account_id", name="uq_cost_limits_scope_account"),
    )


def downgrade() -> None:
    op.drop_table("cost_limits")
