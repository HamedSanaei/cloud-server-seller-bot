"""Business-log outbox for the private operator Telegram channel

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-13

Adds ``business_log_events``: the durable outbox behind the private Telegram
logger channel. Business events are ENQUEUED here (unique ``event_key``) and
delivered by a worker with bounded retry/backoff, so a Telegram outage can
never affect checkout, wallet settlement, provider ordering or
reconciliation. ``status`` + ``next_attempt_at`` drive the claim/backoff
state machine (PENDING -> SENDING -> SENT | RETRY -> ABANDONED).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "business_log_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("event_key", sa.String(200), nullable=False, unique=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
    )
    op.create_index(
        "ix_business_log_events_due",
        "business_log_events",
        ["status", "next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_business_log_events_due", table_name="business_log_events")
    op.drop_table("business_log_events")
