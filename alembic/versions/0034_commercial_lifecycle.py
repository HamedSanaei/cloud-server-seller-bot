"""Commercial service lifecycle state (PROD-HARDENING)

Revision ID: 0034
Revises: 0033
Create Date: 2026-09-13

Adds ``renewals.grace_until``: the end of the payable window once a service's
period expired unpaid.

The commercial state itself already lives in ``renewals.status`` (extended with
``payment_due`` / ``grace_period`` / ``suspended`` / ``expired`` — plain VARCHAR,
so no schema change is needed for new values). What was missing is a durable
"pay by" instant: without it the grace deadline would have to be re-derived
from a provider timestamp on every check, which makes the customer's deadline
move whenever the provider's date drifts.

A single nullable column is deliberately all this migration adds. Confirmation
tokens and Telegram prompts stay in Redis (they are disposable session state),
and a service's renewal PRICE keeps living on the existing
``renewals.customer_price_minor`` — never a current provider quote.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "renewals",
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("renewals", "grace_until")
