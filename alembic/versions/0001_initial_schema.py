"""initial schema with UUID primary keys

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create all initial tables with UUID primary keys."""
    # Enable UUID extension
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    op.create_table(
        "users",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("terms_accepted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("username", name="uq_users_username"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_username", "users", ["username"])

    op.create_table(
        "wallets",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("balance", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"])

    op.create_table(
        "providers",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("region", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("name", name="uq_providers_name"),
    )
    op.create_index("ix_providers_name", "providers", ["name"])

    op.create_table(
        "provider_accounts",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column(
            "provider_id",
            postgresql.UUID(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("provider_id", "user_id", name="uq_provider_accounts_provider_user"),
    )
    op.create_index("ix_provider_accounts_provider_id", "provider_accounts", ["provider_id"])
    op.create_index("ix_provider_accounts_user_id", "provider_accounts", ["user_id"])

    op.create_table(
        "catalog",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "provider_id",
            postgresql.UUID(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_plan_id", sa.String(), nullable=False),
        sa.Column("provider_location_id", sa.String(), nullable=False),
        sa.Column("architecture", sa.String(), nullable=False),
        sa.Column("vcpu", sa.Integer(), nullable=False),
        sa.Column("memory_mb", sa.Integer(), nullable=False),
        sa.Column("disk_gb", sa.Integer(), nullable=False),
        sa.Column("price_per_quantum", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
        sa.Column("quantum_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("extra_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "provider_id",
            "provider_plan_id",
            "provider_location_id",
            name="uq_catalog_provider_plan_location",
        ),
    )
    op.create_index("ix_catalog_provider_id", "catalog", ["provider_id"])
    op.create_index("ix_catalog_enabled", "catalog", ["enabled"])

    op.create_table(
        "servers",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_id",
            postgresql.UUID(),
            sa.ForeignKey("providers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_account_id",
            postgresql.UUID(),
            sa.ForeignKey("provider_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "catalog_id",
            postgresql.UUID(),
            sa.ForeignKey("catalog.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("state", sa.String(), nullable=False, server_default="requested"),
        sa.Column("provider_server_id", sa.String(), nullable=True),
        sa.Column("ipv4", sa.String(), nullable=True),
        sa.Column("ipv6", sa.String(), nullable=True),
        sa.Column("price_per_quantum", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="EUR"),
        sa.Column("quantum_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            onupdate=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_servers_user_id", "servers", ["user_id"])
    op.create_index("ix_servers_provider_id", "servers", ["provider_id"])
    op.create_index("ix_servers_provider_account_id", "servers", ["provider_account_id"])
    op.create_index("ix_servers_state", "servers", ["state"])
    op.create_index("ix_servers_provider_server_id", "servers", ["provider_server_id"])

    # Create outbox table for transactional event publishing
    op.create_table(
        "outbox",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_outbox_processed_at", "outbox", ["processed_at"])
    op.create_index("ix_outbox_event_type", "outbox", ["event_type"])

    # Create ledger table for immutable wallet transactions
    op.create_table(
        "ledger",
        sa.Column(
            "id", postgresql.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entry_type", sa.String(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("reference_type", sa.String(), nullable=True),
        sa.Column("reference_id", postgresql.UUID(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("wallet_id", "idempotency_key", name="uq_ledger_wallet_idempotency"),
    )
    op.create_index("ix_ledger_wallet_id", "ledger", ["wallet_id"])
    op.create_index("ix_ledger_idempotency_key", "ledger", ["idempotency_key"])


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table("ledger")
    op.drop_table("outbox")
    op.drop_table("servers")
    op.drop_table("catalog")
    op.drop_table("provider_accounts")
    op.drop_table("wallets")
    op.drop_table("users")
    op.drop_table("providers")
    op.execute('DROP EXTENSION IF EXISTS "uuid-ossp"')
