"""Add access grants without changing resource owners or any financial rows.

Revision ID: 0061
Revises: 0060
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_access_grants",
        sa.Column("id", postgresql.UUID(), primary_key=True),
        sa.Column(
            "server_id",
            postgresql.UUID(),
            sa.ForeignKey("servers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "delegate_user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("binding", sa.String(64), nullable=False),
        sa.Column("operations", postgresql.JSONB(), nullable=False),
        sa.Column(
            "granted_by",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("server_id", "delegate_user_id", name="uq_server_access_delegate"),
        sa.CheckConstraint("delegate_user_id <> owner_user_id", name="ck_server_access_not_owner"),
    )


def downgrade() -> None:
    # Keep grants/audit evidence during a code rollback. No destructive downgrade.
    pass
