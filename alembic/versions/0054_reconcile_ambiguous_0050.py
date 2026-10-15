"""Reconcile both deployed physical meanings of revision 0050, without restamping.

Revision ID: 0054
Revises: 0053

0050 remains the deployed staging revision. A database carrying the main flavor
of that same identifier traverses 0051-0053 before this additive bridge. Inspect
physical objects, not the ambiguous version label; never rewrite financial facts.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | None = None
depends_on: str | None = None


def _add_missing(table: str, column: sa.Column) -> bool:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name in columns:
        return False
    op.add_column(table, column)
    return True


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0054 requires online PostgreSQL physical-schema inspection")
    _add_missing("users", sa.Column("phone_number", sa.String(16), nullable=True))
    _add_missing("users", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True))
    _add_missing("users", sa.Column("national_id", sa.String(10), nullable=True))
    if _add_missing(
        "servers", sa.Column("billing_started_at", sa.DateTime(timezone=True), nullable=True)
    ):
        # Preserve the historical staging billing anchor, not migration time.
        op.execute(
            sa.text("""
            UPDATE servers SET billing_started_at = created_at AT TIME ZONE 'UTC'
            WHERE billing_model = 'hourly' AND provider_server_id IS NOT NULL
              AND state NOT IN ('requested', 'provisioning')
        """)
        )
    _add_missing("payment_sessions", sa.Column("payment_details", JSONB(), nullable=True))
    _add_missing("payment_sessions", sa.Column("redirect_url", sa.Text(), nullable=True))
    _add_missing("payment_sessions", sa.Column("tracking_code", sa.Text(), nullable=True))
    if not sa.inspect(bind).has_table("payment_gateway_settings"):
        op.create_table(
            "payment_gateway_settings",
            sa.Column("key", sa.String(32), primary_key=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("updated_by", sa.UUID(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        )
    # Refuse conflicting evidence atomically, without logging financial values.
    conflict = bind.execute(
        sa.text("""
        SELECT EXISTS (SELECT 1 FROM payment_sessions WHERE
            (payment_details IS NOT NULL AND jsonb_typeof(payment_details) <> 'object') OR
            (NULLIF(redirect_url, '') IS NOT NULL AND
             NULLIF(payment_details->>'payment_url', '') IS NOT NULL AND
             redirect_url <> payment_details->>'payment_url') OR
            (NULLIF(tracking_code, '') IS NOT NULL AND
             NULLIF(payment_details->>'tracking_code', '') IS NOT NULL AND
             tracking_code <> payment_details->>'tracking_code'))
    """)
    ).scalar_one()
    if conflict:
        raise RuntimeError("0054 found conflicting payment evidence; reconcile before retrying")
    for column, key in (("redirect_url", "payment_url"), ("tracking_code", "tracking_code")):
        # Identifiers are fixed migration constants, never external input.
        op.execute(
            sa.text(f"""
            UPDATE payment_sessions SET payment_details = jsonb_set(
                COALESCE(payment_details, '{{}}'::jsonb), '{{{key}}}', to_jsonb({column}))
            WHERE NULLIF({column}, '') IS NOT NULL
              AND NULLIF(payment_details->>'{key}', '') IS NULL
        """)
        )
        op.execute(
            sa.text(f"""
            UPDATE payment_sessions SET {column} = payment_details->>'{key}'
            WHERE NULLIF({column}, '') IS NULL
              AND NULLIF(payment_details->>'{key}', '') IS NOT NULL
        """)
        )
    names = {i["name"] for i in sa.inspect(bind).get_indexes("payment_sessions")}
    canonical = "uq_atlaspay_merchant_ref"
    legacy = "uq_atlaspay_intent"
    if canonical not in names and legacy in names:
        op.execute(sa.text("ALTER INDEX uq_atlaspay_intent RENAME TO uq_atlaspay_merchant_ref"))
    elif canonical not in names:
        op.create_index(
            canonical,
            "payment_sessions",
            ["gateway_key", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("gateway_key = 'atlaspay'"),
        )
    # Every non-idempotent gateway must reserve its merchant reference before
    # POST. Existing duplicate invoices are evidence, never rows to discard.
    # PostgreSQL aborts this whole bridge if any historical key is ambiguous.
    if "uq_payment_sessions_gateway_idempotency" not in names:
        op.create_index(
            "uq_payment_sessions_gateway_idempotency",
            "payment_sessions",
            ["gateway_key", "idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    raise RuntimeError("0054 is forward-only: preserve identity, payment and prepaid evidence")
