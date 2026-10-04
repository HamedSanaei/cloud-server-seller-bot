"""Own-contact identity, durable payment switches and prepaid activation anchor.

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone_number", sa.String(16), nullable=True))
    op.add_column(
        "users", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("national_id", sa.String(10), nullable=True))
    op.add_column(
        "servers", sa.Column("billing_started_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Historical usage was anchored to created_at. Preserve that anchor and its
    # deterministic ledger keys; new requests start billing only at activation.
    op.execute(
        sa.text("""
        UPDATE servers SET billing_started_at = created_at AT TIME ZONE 'UTC'
        WHERE billing_model = 'hourly' AND provider_server_id IS NOT NULL
          AND state NOT IN ('requested', 'provisioning')
    """)
    )
    op.add_column(
        "payment_sessions", sa.Column("payment_details", postgresql.JSONB(), nullable=True)
    )
    op.create_index(
        "uq_atlaspay_merchant_ref",
        "payment_sessions",
        ["gateway_key", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("gateway_key = 'atlaspay'"),
    )
    op.create_table(
        "payment_gateway_settings",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("payment_gateway_settings")
    op.drop_index("uq_atlaspay_merchant_ref", table_name="payment_sessions")
    op.drop_column("payment_sessions", "payment_details")
    op.drop_column("servers", "billing_started_at")
    op.drop_column("users", "national_id")
    op.drop_column("users", "phone_verified_at")
    op.drop_column("users", "phone_number")
