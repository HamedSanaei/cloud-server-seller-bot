"""Leaseweb multi-credential-account routing (LEASEWEB-MULTIACCOUNT)

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-14

Three additions, all nullable-or-new so no existing row is rewritten lossily:

1. ``provider_routes`` — durable, provider-neutral knowledge of which
   credential account can serve which location. Unique on
   ``(provider_key, credential_account_id, location_id)`` so a sync run is
   idempotent, plus an index on ``(provider_key, location_id)`` for the
   checkout path that resolves a location's fulfillment account.

2. ``provider_orders.credential_account_id`` — the account PINNED before the
   chargeable POST. Authoritative for recovery: Leaseweb account-order
   inventories are credential-scoped, so searching another account could
   never find the order (and must never be used to re-POST it).

3. ``servers.credential_account_id`` — the account that OWNS the VPS, so every
   later management/reconciliation call is addressed with the right key even
   after the location's active route changes.

Both new columns hold a STABLE, NON-SECRET account id (``lw-eu``); no API key,
token or header value is ever persisted. Existing rows are backfilled to the
legacy account id ``default`` so pre-multi-account Leaseweb servers and orders
stay fully routable and manageable — nothing becomes un-routable.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | None = None
depends_on: str | None = None

#: The account id the deprecated single-credential form maps onto
#: (``cloud_platform.providers.routing.DEFAULT_CREDENTIAL_ACCOUNT``).
_LEGACY_ACCOUNT_ID = "default"


def upgrade() -> None:
    op.create_table(
        "provider_routes",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column(
            "credential_account_id",
            sa.String(length=64),
            server_default=_LEGACY_ACCOUNT_ID,
            nullable=False,
        ),
        sa.Column("location_id", sa.String(length=32), nullable=False),
        sa.Column(
            "state",
            sa.String(length=32),
            server_default="transient_unknown",
            nullable=False,
        ),
        sa.Column("account_state", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column(
            "product_ids",
            JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("last_error_class", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_key",
            "credential_account_id",
            "location_id",
            name="uq_provider_routes_account_location",
        ),
    )
    op.create_index(
        "ix_provider_routes_provider_location",
        "provider_routes",
        ["provider_key", "location_id"],
    )

    op.add_column(
        "provider_orders",
        sa.Column("credential_account_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "servers",
        sa.Column("credential_account_id", sa.String(length=64), nullable=True),
    )

    # Backfill: pre-multi-account Leaseweb rows belong to the legacy account, so
    # they resolve without operator action. Non-Leaseweb providers keep NULL
    # (they never had credential accounts) and are routed logically.
    #
    # Expressed as Core UPDATE statements (not raw SQL text) so the migration
    # safety gate can analyse exactly which rows and columns are touched.
    orders = sa.table(
        "provider_orders",
        sa.column("provider_key", sa.String),
        sa.column("credential_account_id", sa.String),
    )
    op.execute(
        orders.update()
        .where(orders.c.provider_key == "leaseweb")
        .where(orders.c.credential_account_id.is_(None))
        .values(credential_account_id=_LEGACY_ACCOUNT_ID)
    )
    servers = sa.table(
        "servers",
        sa.column("provider_id", UUID(as_uuid=True)),
        sa.column("credential_account_id", sa.String),
    )
    providers = sa.table(
        "providers",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("name", sa.String),
    )
    op.execute(
        servers.update()
        .where(servers.c.credential_account_id.is_(None))
        .where(
            servers.c.provider_id.in_(
                sa.select(providers.c.id).where(providers.c.name == "leaseweb")
            )
        )
        .values(credential_account_id=_LEGACY_ACCOUNT_ID)
    )


def downgrade() -> None:
    op.drop_column("servers", "credential_account_id")
    op.drop_column("provider_orders", "credential_account_id")
    op.drop_index("ix_provider_routes_provider_location", table_name="provider_routes")
    op.drop_table("provider_routes")
