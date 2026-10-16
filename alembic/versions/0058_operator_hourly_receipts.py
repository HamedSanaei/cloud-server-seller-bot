"""Allow corporate hourly receipts without inventing a personal wallet.

Revision ID: 0058
Revises: 0057
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    table = "prepaid_hourly_periods"
    op.add_column(
        table, sa.Column("nominal_currency", sa.String(3), nullable=False, server_default="USD")
    )
    op.add_column(
        table,
        sa.Column(
            "funding_source", sa.String(32), nullable=False, server_default="customer_wallet"
        ),
    )
    for name, target in (
        ("funding_operator_id", "users.id"),
        ("operator_grant_id", "operator_funding_grants.id"),
        ("operator_expense_id", "operator_expenses.id"),
    ):
        op.add_column(
            table,
            sa.Column(name, sa.UUID(), sa.ForeignKey(target, ondelete="RESTRICT"), nullable=True),
        )
    for name in ("wallet_id", "irt_minor", "fx_snapshot"):
        op.alter_column(table, name, nullable=True)
    op.drop_constraint("ck_prepaid_hourly_positive", table, type_="check")
    op.create_check_constraint(
        "ck_prepaid_hourly_positive", table, "usd_minor > 0 AND period_end > period_start"
    )
    op.create_check_constraint(
        "ck_prepaid_hourly_funding_evidence",
        table,
        "(funding_source = 'customer_wallet' AND nominal_currency = 'USD' "
        "AND wallet_id IS NOT NULL "
        "AND irt_minor IS NOT NULL AND irt_minor > 0 AND fx_snapshot IS NOT NULL "
        "AND jsonb_typeof(fx_snapshot) = 'object' "
        "AND funding_operator_id IS NULL AND operator_grant_id IS NULL "
        "AND operator_expense_id IS NULL) OR "
        "(funding_source = 'operator' AND wallet_id IS NULL AND irt_minor IS NULL "
        "AND fx_snapshot IS NULL AND funding_operator_id IS NOT NULL "
        "AND operator_grant_id IS NOT NULL "
        "AND (status != 'paid' OR operator_expense_id IS NOT NULL))",
    )


def downgrade() -> None:
    # Retain accounting evidence; retirement requires an audited forward change.
    pass
