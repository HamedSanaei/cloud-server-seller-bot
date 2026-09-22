"""Sellable-offer billing model (STOREFRONT-REWORK).

Revision ID: 0041
Revises: 0040
Create Date: 2026-09-23

WHAT THIS REVISION DOES
------------------------
Adds ``sellable_offers.billing_model`` (inspector-guarded): the commercial
terms of the offer — ``prepaid_monthly_fixed`` (monthly VPS, ordered through
the ordering API) or ``hourly`` (usage-based cloud instances, created through
the instance API and billed by time accrual). Monthly and hourly products
must never share billing semantics, so the model carries the distinction and
checkout branches on it (never on a provider name).

Existing rows keep the monthly default: every offer written before this
revision was sold prepaid-monthly. No backfill beyond the column default.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "sellable_offers"
COLUMN = "billing_model"
DEFAULT = "prepaid_monthly_fixed"


def _notify(message: str) -> None:
    """Make the revision visible in the deployment log."""
    print(f"[migration {revision}] {message}", flush=True)


def upgrade() -> None:
    columns = {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN in columns:
        _notify("sellable_offers.billing_model already exists — nothing to do")
        return
    op.add_column(
        TABLE,
        sa.Column(COLUMN, sa.String(length=32), nullable=False, server_default=DEFAULT),
    )
    _notify("added sellable_offers.billing_model (existing rows stay prepaid-monthly)")


def downgrade() -> None:
    """Refuse: dropping the column would merge two billing models silently."""
    raise RuntimeError(
        "0041 has no automatic downgrade: dropping billing_model would make "
        "hourly and monthly offers indistinguishable. Restore from a backup."
    )
