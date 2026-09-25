"""Durable provider credential-account capacity (LEASEWEB-MULTIACCOUNT).

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-25

WHAT THIS REVISION DOES
-----------------------
Creates ``provider_account_capacity``: one row per
``(provider_key, credential_account_id)`` remembering whether that account can
currently accept NEW billable orders.

WHY IT EXISTS
-------------
Production proved the need. The Frankfurt Sales Organization answered a
well-formed hourly create with a definitive provider refusal::

    errorCode=PC-2031  Customer limit reached  correlationId=07376219-...

Leaseweb publishes no quota/limit endpoint (the official Public Cloud API
schema contains no quota path at all), so capacity can only be LEARNED from a
create refusal. Without a durable home for that fact, the storefront keeps
publishing offers pinned to an account that has just refused an order, and the
next customer hits the same wall.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
No existing row is read, rewritten or deleted, and no capacity state is
invented for any account: the table starts EMPTY, which means "healthy / never
observed" for every account. The state is time bounded (``expires_at``), so a
single refusal cannot permanently disable a credential, and it never affects
management, reconciliation, power or delete flows — only NEW-order
publication, which is why nothing here is backfilled.

The stored facts are non-secret: a provider error code, a provider routing
correlation id, a location id and a product id. No API key, header or request
body is stored anywhere in this table.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "provider_account_capacity",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column("credential_account_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="healthy", nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("location_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=128), nullable=True),
        sa.Column("observations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_key",
            "credential_account_id",
            name="uq_provider_account_capacity_account",
        ),
    )
    op.create_index(
        "ix_provider_account_capacity_provider_state",
        "provider_account_capacity",
        ["provider_key", "state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_account_capacity_provider_state",
        table_name="provider_account_capacity",
    )
    op.drop_table("provider_account_capacity")
