"""add payment_sessions table (unique external identity)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "payment_sessions",
        sa.Column(
            "id",
            PG_UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column("user_id", PG_UUID(), nullable=False),
        sa.Column("gateway_key", sa.String(), nullable=False),
        sa.Column("gateway_payment_id", sa.String(), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=text("'pending'")),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("credited_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
    )
    # Unique external identity: (gateway_key, gateway_payment_id). PostgreSQL
    # treats NULLs as distinct in unique indexes, so sessions that have not
    # yet received an external id are unconstrained.
    op.create_index(
        "uq_payment_sessions_external_id",
        "payment_sessions",
        ["gateway_key", "gateway_payment_id"],
        unique=True,
    )
    op.create_index("ix_payment_sessions_user", "payment_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_sessions_user", table_name="payment_sessions")
    op.drop_index("uq_payment_sessions_external_id", table_name="payment_sessions")
    op.drop_table("payment_sessions")
