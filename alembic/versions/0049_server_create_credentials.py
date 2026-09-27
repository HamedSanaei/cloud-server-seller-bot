"""Store provider-issued server passwords encrypted until one owner-only read.

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "server_create_credentials",
        sa.Column("server_id", sa.UUID(), primary_key=True),
        sa.Column("provider_server_id", sa.String(), nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=True),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("key_id", sa.String(length=12), nullable=False),
        sa.Column("algorithm", sa.String(length=32), nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("server_create_credentials")
