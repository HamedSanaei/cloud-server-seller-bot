"""Operator funding: grants, immutable expenses and cutover receipts.

Revision ID: 0056
Revises: 0055

Additive only. It appends the ``servers`` funding columns (default
``customer_wallet`` so every existing row and every existing customer path is
unchanged) and creates three new, RESTRICT-referenced tables:

* ``operator_funding_grants`` — idempotent authorization evidence for
  organisation-funded provider work;
* ``operator_expenses`` — the immutable cost of exactly one settled grant;
* ``operator_funding_cutovers`` — immutable receipts for a LATER, explicitly
  reviewed funding-source rollout.

No data cutover, no role-based bulk update and no wallet mutation happens here.
The expense and cutover tables are protected against UPDATE/DELETE by a
trigger. Organisation funding is authorisation evidence and organisation cost,
NOT customer-wallet credit and NOT a synthetic cash payment.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | None = None
depends_on: str | None = None

_PURPOSE_CHECK = "purpose IN ('monthly_create', 'monthly_renew', 'hourly_usage', 'final_usage')"
_CURRENCY_CHECK = "char_length(nominal_currency) = 3 AND nominal_currency = upper(nominal_currency)"

_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_operator_financial_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % not permitted', TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column(
            "funding_source",
            sa.String(length=32),
            nullable=False,
            server_default="customer_wallet",
        ),
    )

    op.add_column(
        "servers",
        sa.Column(
            "funding_operator_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_servers_funding_source", "servers", "funding_source IN ('customer_wallet', 'operator')"
    )
    op.create_check_constraint(
        "ck_servers_funding_owner",
        "servers",
        "(funding_source = 'customer_wallet' AND funding_operator_id IS NULL) OR "
        "(funding_source = 'operator' AND funding_operator_id IS NOT NULL "
        "AND funding_operator_id = user_id)",
    )

    op.create_table(
        "operator_funding_grants",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("operation_key", sa.String(length=200), nullable=False, unique=True),
        sa.Column(
            "operator_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("resource_key", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("nominal_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("nominal_currency", sa.String(length=3), nullable=False),
        sa.Column("provider_cost_snapshot", JSONB(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="authorized"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "nominal_amount_minor > 0",
            name="ck_operator_funding_grants_amount_positive",
        ),
        sa.CheckConstraint(
            "owner_user_id = operator_user_id",
            name="ck_operator_funding_grants_self_owner",
        ),
        sa.CheckConstraint(
            "state IN ('authorized', 'settled', 'cancelled')",
            name="ck_operator_funding_grants_state",
        ),
        sa.CheckConstraint(
            _PURPOSE_CHECK,
            name="ck_operator_funding_grants_purpose",
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK,
            name="ck_operator_funding_grants_currency",
        ),
    )

    op.create_table(
        "operator_expenses",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column(
            "grant_id",
            sa.UUID(),
            sa.ForeignKey("operator_funding_grants.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("operation_key", sa.String(length=220), nullable=False, unique=True),
        sa.Column(
            "owner_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "operator_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("resource_key", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("nominal_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("nominal_currency", sa.String(length=3), nullable=False),
        sa.Column("provider_cost_snapshot", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "nominal_amount_minor > 0",
            name="ck_operator_expenses_amount_positive",
        ),
        sa.CheckConstraint(
            "owner_user_id = operator_user_id",
            name="ck_operator_expenses_self_owner",
        ),
        sa.CheckConstraint(
            _PURPOSE_CHECK,
            name="ck_operator_expenses_purpose",
        ),
        sa.CheckConstraint(
            _CURRENCY_CHECK,
            name="ck_operator_expenses_currency",
        ),
    )

    op.create_table(
        "operator_funding_cutovers",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("uuid_generate_v4()")),
        sa.Column("operation_key", sa.String(length=200), nullable=False, unique=True),
        sa.Column(
            "server_id",
            sa.UUID(),
            sa.ForeignKey("servers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "operator_user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("prior_funding_source", sa.String(length=32), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(
            "prior_funding_source IN ('customer_wallet', 'operator')",
            name="ck_operator_funding_cutovers_prior_source",
        ),
    )

    op.execute(_IMMUTABLE_FUNCTION)
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_operator_funding_grant() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'operator funding grants are immutable history';
            END IF;
            IF ROW(NEW.id, NEW.operation_key, NEW.operator_user_id, NEW.owner_user_id,
                   NEW.resource_key, NEW.purpose, NEW.nominal_amount_minor,
                   NEW.nominal_currency, NEW.provider_cost_snapshot, NEW.created_at)
               IS DISTINCT FROM
               ROW(OLD.id, OLD.operation_key, OLD.operator_user_id, OLD.owner_user_id,
                   OLD.resource_key, OLD.purpose, OLD.nominal_amount_minor,
                   OLD.nominal_currency, OLD.provider_cost_snapshot, OLD.created_at) THEN
                RAISE EXCEPTION 'operator funding grant financial facts are immutable';
            END IF;
            IF NEW.state IS DISTINCT FROM OLD.state AND NOT
               (OLD.state = 'authorized' AND NEW.state IN ('settled', 'cancelled')) THEN
                RAISE EXCEPTION 'operator funding grant state transition is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER operator_funding_grants_immutable
        BEFORE UPDATE OR DELETE ON operator_funding_grants FOR EACH ROW
        EXECUTE FUNCTION protect_operator_funding_grant()
    """)
    for table_name in ("operator_expenses", "operator_funding_cutovers"):
        op.execute(
            f"CREATE TRIGGER {table_name}_append_only BEFORE UPDATE OR DELETE ON {table_name} "
            "FOR EACH ROW EXECUTE FUNCTION prevent_operator_financial_mutation()"
        )


def downgrade() -> None:
    """Retain all financial evidence, including while tables are empty.

    Retiring funding provenance requires a separately reviewed forward migration.
    Rolling an application image back must never erase its accounting schema.
    """
    pass
