"""add audit_events table (append-only, trigger-protected)

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None

_APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only: % not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql
"""

_APPEND_ONLY_TRIGGER = """
CREATE TRIGGER audit_events_append_only
BEFORE UPDATE OR DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation()
"""


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column(
            "id",
            PG_UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column("actor_type", sa.String(), nullable=False),
        sa.Column("actor_id", PG_UUID(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=text("''")),
        sa.Column(
            "metadata",
            JSONB(),
            nullable=False,
            server_default=text("'{}'::jsonb"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            server_default=text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "ix_audit_events_resource",
        "audit_events",
        ["resource_type", "resource_id"],
    )
    op.create_index("ix_audit_events_actor", "audit_events", ["actor_id"])
    op.execute(_APPEND_ONLY_FUNCTION)
    op.execute(_APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation()")
    op.drop_index("ix_audit_events_actor", table_name="audit_events")
    op.drop_index("ix_audit_events_resource", table_name="audit_events")
    op.drop_table("audit_events")
