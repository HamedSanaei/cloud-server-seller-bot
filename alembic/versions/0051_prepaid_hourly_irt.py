"""Track upcoming paid hours and binding USD-to-IRT conversion evidence.

Revision ID: 0051
Revises: 0050

Existing hourly arrears and fixed monthly contracts retain their original facts.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("servers", sa.Column("prepaid_paid_until", sa.DateTime(), nullable=True))
    op.add_column("servers", sa.Column("prepaid_zero_since", sa.DateTime(), nullable=True))
    op.create_table(
        "prepaid_hourly_periods",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("server_id", sa.UUID(), nullable=False),
        sa.Column("wallet_id", sa.UUID(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("usd_minor", sa.BigInteger(), nullable=False),
        sa.Column("irt_minor", sa.BigInteger(), nullable=False),
        sa.Column("fx_snapshot", JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["wallet_id"], ["wallets.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("server_id", "period_start", name="uq_prepaid_hourly_server_start"),
        sa.CheckConstraint(
            "usd_minor > 0 AND irt_minor > 0 AND period_end > period_start",
            name="ck_prepaid_hourly_positive",
        ),
    )
    op.create_index(
        "ix_prepaid_hourly_pending",
        "prepaid_hourly_periods",
        ["server_id", "status", "period_start"],
    )


def downgrade() -> None:
    raise RuntimeError(
        "0051 cannot be downgraded automatically: paid hour and FX evidence must not be discarded"
    )
