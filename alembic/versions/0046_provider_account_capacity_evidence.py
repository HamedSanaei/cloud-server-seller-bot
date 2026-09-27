"""Append-only evidence behind credential-account capacity (LEASEWEB-MULTIACCOUNT).

Revision ID: 0046
Revises: 0045
Create Date: 2026-09-26

WHAT THIS REVISION DOES
-----------------------
Creates ``provider_account_capacity_events``: the append-only evidence log
behind ``provider_account_capacity``. One row per observed refusal (live or
recovered from history) and per operator clear.

WHY IT EXISTS
-------------
Production proved two gaps in the first capacity release:

1. The capacity table started EMPTY, so a Sales Organization that had already
   refused a create BEFORE the feature existed was treated as eligible again
   and was handed the next customer order — which the provider refused with
   the same ``PC-2031``. Recovering that history is a reconciliation, not an
   operator SQL statement, and it must be idempotent: the partial unique index
   on ``(provider_key, credential_account_id, kind, source_ref)`` makes
   re-running it append nothing.
2. ``expires_at`` was read as \"eligible again\". An elapsed cooling window is
   NOT evidence that the provider limit lifted, so the state vocabulary gains
   ``unknown_after_limit`` and this log keeps the incident's own timeline
   (``observed_at`` / ``expires_at``) separate from the current verdict.

WHAT IT DOES NOT DO
-------------------
No existing row is read, rewritten or deleted. In particular the capacity
table is NOT backfilled here: parsing a stored provider error string belongs to
the audited classifier in the provider module (``is_capacity_exhausted``), so
the deterministic recovery runs as an idempotent reconciliation routine with
regression coverage instead of a one-off SQL statement. The table starts empty
and means \"no evidence recorded yet\".
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "provider_account_capacity_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("uuid_generate_v4()"),
            nullable=False,
        ),
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column("credential_account_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("location_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=128), nullable=True),
        sa.Column("source_ref", sa.String(length=128), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provider_account_capacity_events_account",
        "provider_account_capacity_events",
        ["provider_key", "credential_account_id", "created_at"],
    )
    # The idempotency anchor of the historical reconciliation: one evidence row
    # per (account, kind, provider operation). Live refusals carry no
    # source_ref and are therefore never deduplicated.
    op.create_index(
        "uq_provider_account_capacity_events_source",
        "provider_account_capacity_events",
        ["provider_key", "credential_account_id", "kind", "source_ref"],
        unique=True,
        postgresql_where=sa.text("source_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_provider_account_capacity_events_source",
        table_name="provider_account_capacity_events",
    )
    op.drop_index(
        "ix_provider_account_capacity_events_account",
        table_name="provider_account_capacity_events",
    )
    op.drop_table("provider_account_capacity_events")
