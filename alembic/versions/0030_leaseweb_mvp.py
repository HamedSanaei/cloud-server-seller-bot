"""Leaseweb monthly VPS MVP schema (LEASEWEB-MVP)

Revision ID: 0030
Revises: 0029
Create Date: 2026-09-06

Adds the sellable-offers price book, the provider-order lifecycle table,
renewal records/notifications, and the per-server billing-model gate that
keeps prepaid monthly servers out of the hourly accrual machinery.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- sellable offers: explicit monthly price book ---------------------
    op.create_table(
        "sellable_offers",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column("provider_key", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(64), nullable=False),
        sa.Column("location_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("vcpu", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ram_gb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("disk_gb", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("traffic", sa.String(), nullable=True),
        sa.Column("provider_cost_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("provider_cost_currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column("selling_price_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("selling_currency", sa.String(3), nullable=False, server_default="EUR"),
        sa.Column("billing_parameters", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("provider_available", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "provider_key",
            "product_id",
            "location_id",
            name="uq_sellable_offers_provider_product_location",
        ),
    )
    op.create_index(
        "ix_sellable_offers_browse",
        "sellable_offers",
        ["provider_key", "location_id", "provider_available", "enabled"],
    )

    # --- provider orders: async provisioning lifecycle --------------------
    op.create_table(
        "provider_orders",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(),
            sa.ForeignKey("servers.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("operation_key", sa.String(), nullable=False, unique=True),
        sa.Column("provider_key", sa.String(32), nullable=False),
        sa.Column(
            "offer_id",
            postgresql.UUID(),
            sa.ForeignKey("sellable_offers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider_order_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_submit"),
        sa.Column("delivery_estimate", sa.String(), nullable=True),
        sa.Column("provider_contract_id", sa.String(), nullable=True),
        sa.Column("provider_service_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_provider_orders_status", "provider_orders", ["status", "provider_key"])

    # --- servers: hourly catalog pin becomes optional ----------------------
    # Prepaid monthly servers (LEASEWEB-MVP) pin their offer in
    # sellable_offers (via provider_orders.offer_id), not the hourly catalog.
    # The FK is KEPT (NULL never cascades): only NOT NULL is relaxed.
    op.alter_column("servers", "catalog_id", existing_type=postgresql.UUID(), nullable=True)

    # --- renewals: prepaid monthly lifecycle -------------------------------
    op.create_table(
        "renewals",
        sa.Column(
            "server_id",
            postgresql.UUID(),
            sa.ForeignKey("servers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("provider_contract_id", sa.String(), nullable=True),
        sa.Column("provider_order_ref", sa.String(), nullable=True),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_renewal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("renewal_date_estimated", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("customer_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("auto_charge_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_renewals_due",
        "renewals",
        ["status", "provider_renewal_at"],
    )
    op.create_table(
        "renewal_notifications",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
        sa.Column(
            "server_id",
            postgresql.UUID(),
            sa.ForeignKey("servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("for_period", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "server_id",
            "kind",
            "for_period",
            name="uq_renewal_notifications_server_kind_period",
        ),
    )

    # --- servers: billing model gate + OS ---------------------------------
    op.add_column(
        "servers",
        sa.Column("billing_model", sa.String(32), nullable=False, server_default="hourly"),
    )
    op.add_column("servers", sa.Column("os", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_table("renewal_notifications")
    op.drop_table("renewals")
    op.drop_table("provider_orders")
    op.drop_table("sellable_offers")
    op.drop_column("servers", "os")
    op.drop_column("servers", "billing_model")
    op.alter_column("servers", "catalog_id", existing_type=postgresql.UUID(), nullable=False)
