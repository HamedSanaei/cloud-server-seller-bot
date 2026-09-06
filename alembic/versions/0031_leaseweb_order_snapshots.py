"""Leaseweb ambiguous-order hardening (LEASEWEB-MVP)

Revision ID: 0031
Revises: 0030
Create Date: 2026-09-06

Adds the order-fact snapshots to ``provider_orders`` (exact provider
product id, location, OS, contract term, billing cycle, PROVIDER cost and
customer selling price as SEPARATE snapshots) plus ``post_attempted_at``
(the instant the chargeable POST was sent). These are committed BEFORE the
POST and are what the read-only recovery scan correlates on when a POST
outcome is unknown — the customer selling price is never used to identify
a provider order.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("provider_orders", sa.Column("product_id", sa.String(64), nullable=True))
    op.add_column("provider_orders", sa.Column("location_id", sa.String(32), nullable=True))
    op.add_column("provider_orders", sa.Column("os_name", sa.String(), nullable=True))
    op.add_column("provider_orders", sa.Column("contract_term", sa.String(32), nullable=True))
    op.add_column("provider_orders", sa.Column("billing_cycle", sa.String(32), nullable=True))
    op.add_column(
        "provider_orders", sa.Column("provider_cost_minor", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "provider_orders", sa.Column("provider_cost_currency", sa.String(3), nullable=True)
    )
    op.add_column(
        "provider_orders", sa.Column("selling_price_minor", sa.BigInteger(), nullable=True)
    )
    op.add_column("provider_orders", sa.Column("selling_currency", sa.String(3), nullable=True))
    op.add_column(
        "provider_orders", sa.Column("post_attempted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("provider_orders", "post_attempted_at")
    op.drop_column("provider_orders", "selling_currency")
    op.drop_column("provider_orders", "selling_price_minor")
    op.drop_column("provider_orders", "provider_cost_currency")
    op.drop_column("provider_orders", "provider_cost_minor")
    op.drop_column("provider_orders", "billing_cycle")
    op.drop_column("provider_orders", "contract_term")
    op.drop_column("provider_orders", "os_name")
    op.drop_column("provider_orders", "location_id")
    op.drop_column("provider_orders", "product_id")
