"""Persist immutable approved administrator campaigns and bounded delivery work.

Revision ID: 0059
Revises: 0058
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "admin_message_campaigns",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column(
            "actor_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("recipient_ids", JSONB(), nullable=False),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "actor_id", "idempotency_key", name="uq_admin_message_campaign_actor_key"
        ),
        sa.CheckConstraint("char_length(body) BETWEEN 1 AND 3000", name="ck_admin_campaign_body"),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500", name="ck_admin_campaign_reason"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(recipient_ids) = 'array' AND "
            "jsonb_array_length(recipient_ids) BETWEEN 1 AND 100",
            name="ck_admin_campaign_recipient_count",
        ),
    )
    op.create_table(
        "admin_message_deliveries",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column(
            "campaign_id",
            sa.UUID(),
            sa.ForeignKey("admin_message_campaigns.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("claim_token", sa.UUID()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_admin_message_delivery_recipient"),
        sa.CheckConstraint("telegram_user_id > 0", name="ck_admin_delivery_telegram_identity"),
        sa.CheckConstraint("attempts >= 0", name="ck_admin_delivery_attempts"),
        sa.CheckConstraint(
            "status IN ('pending', 'leased', 'sent', 'failed', 'ambiguous')",
            name="ck_admin_delivery_status",
        ),
    )
    op.create_index(
        "ix_admin_message_delivery_pending",
        "admin_message_deliveries",
        ["status", "next_attempt_at"],
    )
    op.execute("""
        CREATE TRIGGER admin_message_campaigns_immutable
        BEFORE UPDATE OR DELETE ON admin_message_campaigns FOR EACH ROW
        EXECUTE FUNCTION prevent_operator_financial_mutation()
    """)


def downgrade() -> None:
    # Approved message history and audit evidence are retained on rollback.
    pass
