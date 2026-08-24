"""ip_addresses table (M13-008)

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "ip_addresses",
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
        sa.Column("provider_ip_id", sa.String(64), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="floating"),
        sa.Column("location_id", sa.String(32), nullable=True),
        sa.Column(
            "server_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("servers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("provider_key", "provider_ip_id", name="uq_ips_provider"),
    )
    op.create_index("ix_ip_addresses_user_id", "ip_addresses", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_ip_addresses_user_id", table_name="ip_addresses")
    op.drop_table("ip_addresses")
