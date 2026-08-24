"""networks table (M13-010)

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "networks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("uuid_generate_v4()"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_key", sa.String(32), nullable=False),
        sa.Column("provider_network_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("ip_range", sa.String(32), nullable=False),
        sa.Column("server_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("location_id", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("provider_key", "provider_network_id", name="uq_networks_provider"),
    )
    op.create_index("ix_networks_user_id", "networks", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_networks_user_id", table_name="networks")
    op.drop_table("networks")
