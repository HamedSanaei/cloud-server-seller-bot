"""Allow monthly and hourly products to coexist at the same provider/location.

Revision ID: 0048
Revises: 0047

Existing offers retain their billing_model (0041 defaulted historic rows to
prepaid_monthly_fixed). Only the unique constraint changes: no prices, offer
identities, operator settings or provider observations are rewritten.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "sellable_offers"
_OLD = "uq_sellable_offers_provider_product_location"
_NEW = "uq_sellable_offers_provider_product_location_billing"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    constraints = {str(entry["name"]) for entry in inspector.get_unique_constraints(_TABLE)}
    if _NEW in constraints:
        return
    if _OLD in constraints:
        op.drop_constraint(_OLD, _TABLE, type_="unique")
    op.create_unique_constraint(
        _NEW, _TABLE, ["provider_key", "product_id", "location_id", "billing_model"]
    )


def downgrade() -> None:
    raise RuntimeError(
        "0048 has no automatic downgrade: monthly and hourly offers may share "
        "a provider/product/location identity. Restore from a backup."
    )
