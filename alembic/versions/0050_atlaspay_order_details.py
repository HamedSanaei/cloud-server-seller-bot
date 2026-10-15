"""Retain AtlasPay's one-time payment link and customer tracking code.

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("payment_sessions", sa.Column("redirect_url", sa.Text(), nullable=True))
    op.add_column("payment_sessions", sa.Column("tracking_code", sa.Text(), nullable=True))
    op.create_index(
        "uq_atlaspay_intent",
        "payment_sessions",
        ["gateway_key", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("gateway_key = 'atlaspay'"),
    )


def downgrade() -> None:
    op.drop_index("uq_atlaspay_intent", table_name="payment_sessions")
    op.drop_column("payment_sessions", "tracking_code")
    op.drop_column("payment_sessions", "redirect_url")
