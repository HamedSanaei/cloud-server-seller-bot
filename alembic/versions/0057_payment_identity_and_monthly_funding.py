"""Pin concrete provider payments and monthly funding evidence.

Revision ID: 0057
Revises: 0056

Additive only: historical customer contracts retain their existing funding path.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("payment_sessions", sa.Column("provider_payment_id", sa.String(), nullable=True))
    op.create_unique_constraint(
        "uq_payment_sessions_provider_payment",
        "payment_sessions",
        ["gateway_key", "provider_payment_id"],
    )
    for table in ("provider_orders", "renewals"):
        op.add_column(
            table,
            sa.Column(
                "funding_source", sa.String(32), nullable=False, server_default="customer_wallet"
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "funding_operator_id",
                sa.UUID(),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=True,
            ),
        )
        op.add_column(table, sa.Column("provider_cost_snapshot", JSONB(), nullable=True))
        op.create_check_constraint(
            f"ck_{table}_funding_source", table, "funding_source IN ('customer_wallet', 'operator')"
        )
    op.add_column(
        "provider_orders", sa.Column("funding_grant_operation_key", sa.String(200), nullable=True)
    )
    op.create_check_constraint(
        "ck_provider_orders_funding_evidence",
        "provider_orders",
        "(funding_source = 'customer_wallet' AND funding_operator_id IS NULL "
        "AND funding_grant_operation_key IS NULL) OR "
        "(funding_source = 'operator' AND funding_operator_id IS NOT NULL "
        "AND funding_grant_operation_key IS NOT NULL AND provider_cost_snapshot IS NOT NULL "
        "AND jsonb_typeof(provider_cost_snapshot) = 'object')",
    )
    op.create_check_constraint(
        "ck_renewals_funding_evidence",
        "renewals",
        "(funding_source = 'customer_wallet' AND funding_operator_id IS NULL) OR "
        "(funding_source = 'operator' AND funding_operator_id IS NOT NULL "
        "AND provider_cost_snapshot IS NOT NULL "
        "AND jsonb_typeof(provider_cost_snapshot) = 'object')",
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_provider_payment_identity() RETURNS trigger AS $$
        BEGIN
            IF OLD.provider_payment_id IS NOT NULL AND
               NEW.provider_payment_id IS DISTINCT FROM OLD.provider_payment_id THEN
                RAISE EXCEPTION 'provider payment identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER payment_sessions_provider_payment_identity
        BEFORE UPDATE ON payment_sessions FOR EACH ROW
        EXECUTE FUNCTION protect_provider_payment_identity()
    """)


def downgrade() -> None:
    # A reviewed forward migration is required to retire financial provenance.
    pass
