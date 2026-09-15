"""Leaseweb offer credential-account provenance (LEASEWEB-MULTIACCOUNT).

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-16

One Leaseweb API key only sees its own Sales Organization's locations, so the
seller's inventory is identified by ``(provider_account_id, location, product)``
— the same product at a different location, or in a different credential
account, is a DIFFERENT sellable item.

That per-account inventory identity is durable in ``provider_routes`` (unique
on ``provider_key + credential_account_id + location_id``, with the products
that account sells at that location). This revision adds the matching
provenance to the operator-priced, customer-visible offer row so the price book
records WHICH credential supplied each observation.

The column is NULLABLE and additive: no existing row is rewritten, every
provider without credential accounts keeps NULL, and pre-multi-account leaseweb
offers are backfilled to the legacy account id ``default`` (the same convention
migration 0035 used for servers and provider orders) so nothing becomes
un-routable. No downgrade destroys data automatically beyond dropping the
column, which is operator-gated.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | None = None
depends_on: str | None = None

#: Account id the deprecated single-credential form maps onto
#: (``cloud_platform.providers.routing.DEFAULT_CREDENTIAL_ACCOUNT``).
_LEGACY_ACCOUNT_ID = "default"


def upgrade() -> None:
    op.add_column(
        "sellable_offers", sa.Column("provider_account_id", sa.String(length=64), nullable=True)
    )

    # Backfill only the rows that predate multi-account routing and can only
    # have come from the single legacy credential. Rows belonging to providers
    # that never had credential accounts keep NULL (they are routed logically).
    offers = sa.table(
        "sellable_offers",
        sa.column("provider_key", sa.String),
        sa.column("provider_account_id", sa.String),
    )
    op.execute(
        offers.update()
        .where(offers.c.provider_key == "leaseweb")
        .where(offers.c.provider_account_id.is_(None))
        .values(provider_account_id=_LEGACY_ACCOUNT_ID)
    )


def downgrade() -> None:
    op.drop_column("sellable_offers", "provider_account_id")
