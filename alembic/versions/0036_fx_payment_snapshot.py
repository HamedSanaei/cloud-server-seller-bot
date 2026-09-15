"""Cross-currency payment snapshot (FX recharge).

Revision ID: 0036
Revises: 0034
Create Date: 2026-09-15

Branch note: this revision was developed against a tree where the unrelated
Leaseweb multi-account migration 0035 also existed. On the FX-only branch it
is rebased onto 0034 so the chain stays linear with a single head. If 0035
lands on main first, re-point ``down_revision`` back to ``"0035"`` (or add a
merge migration) at merge time — the column set itself is unchanged.

``payment_sessions.amount_minor``/``currency`` remain the GATEWAY settlement
amount (what the provider invoice charges and what inquiry verifies).
Cross-currency recharges additionally persist the frozen WALLET credit side
plus the FX conversion audit trail, so the callback and reconciliation
verify the settlement side and credit the frozen credit side — they never
fetch a new rate.

All new columns are NULLABLE: legacy same-currency sessions stay readable
(credit == settlement) and no historical wallet/ledger record is modified.
No backfill converts balances; no downgrade destroys data automatically
(the downgrade below drops the columns, which is operator-gated).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: str | None = "0034"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "payment_sessions", sa.Column("credit_amount_minor", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "payment_sessions", sa.Column("credit_currency", sa.String(length=3), nullable=True)
    )
    op.add_column("payment_sessions", sa.Column("fx_source", sa.String(length=32), nullable=True))
    op.add_column("payment_sessions", sa.Column("fx_rate", sa.String(length=64), nullable=True))
    op.add_column("payment_sessions", sa.Column("fx_path", sa.String(length=256), nullable=True))
    op.add_column(
        "payment_sessions", sa.Column("fx_observed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("payment_sessions", sa.Column("fx_proxy", sa.Boolean(), nullable=True))
    op.add_column(
        "payment_sessions", sa.Column("fx_proxy_asset", sa.String(length=16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("payment_sessions", "fx_proxy_asset")
    op.drop_column("payment_sessions", "fx_proxy")
    op.drop_column("payment_sessions", "fx_observed_at")
    op.drop_column("payment_sessions", "fx_path")
    op.drop_column("payment_sessions", "fx_rate")
    op.drop_column("payment_sessions", "fx_source")
    op.drop_column("payment_sessions", "credit_currency")
    op.drop_column("payment_sessions", "credit_amount_minor")
