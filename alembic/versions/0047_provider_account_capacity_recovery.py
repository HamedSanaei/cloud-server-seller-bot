"""Automated canary recovery for credential-account capacity (LEASEWEB-MULTIACCOUNT).

Revision ID: 0047
Revises: 0046
Create Date: 2026-09-26

WHAT THIS REVISION DOES
-----------------------
Adds the columns of the AUTOMATIC capacity recovery controller to
``provider_account_capacity`` — all nullable/defaulted, so every existing row
keeps its exact meaning:

* ``baseline_instance_count`` / ``baseline_instance_ids_hash`` /
  ``baseline_observed_at``: the read-only inventory the account held when the
  refusal was learned. A later census with a LOWER count is local evidence that
  an instance was freed and brings the next recovery window forward.
* ``recovery_attempts`` / ``last_recovery_attempt_at`` /
  ``next_recovery_attempt_at``: the canary attempt counter and its exponential
  backoff schedule (15 m / 30 m / 1 h / 2 h / capped at 6 h, configurable).
* ``canary_lease_ref`` / ``canary_lease_expires_at``: the DURABLE PostgreSQL
  single-canary lease. Exactly one real customer order per account may be in
  flight as the recovery canary; the lease is acquired with one conditional
  ``UPDATE`` (single winner) and expires on its own if the process dies.
* ``outage_notified_at`` / ``last_reminder_at``: notification bookkeeping for
  the operator feed (the durable outbox UNIQUE ``event_key`` is the dedupe;
  these columns make the DECISION idempotent across concurrent workers too).

WHY IT EXISTS
-------------
The first capacity release could only LEARN a refusal and required the operator
to remember ``leaseweb cloud accounts clear``. A freed quota, a deleted
instance or a raised provider limit therefore left the Cloud storefront
silently unavailable forever. This revision is the storage half of the fix:
normal recovery becomes a bounded, automatic canary attempt, and the manual
clear remains only the emergency override.

WHAT IT DOES NOT DO
-------------------
No row is read, rewritten or deleted; no state string changes. An elapsed
cooling window still never means eligible — the new state
``recovery_candidate`` is only reachable through inventory evidence or the
scheduled backoff, and only a provider ACCEPTANCE (or an operator clear) proves
recovery.

SCHEMA-PARITY NOTE
------------------
Every ``op.add_column`` is written INLINE with a literal table name and a
direct ``sa.Column(...)`` argument. ``tests/unit/test_schema_code_parity.py``
discovers created columns by walking the upgrade-side AST for exactly that
shape, and reads ``revision`` / ``down_revision`` with ``ast.literal_eval``. A
module-level tuple of ``sa.Column(...)`` objects (an ``ast.Call``) therefore
both crashes the revision reader and hides the columns from the parity gate.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | None = None
depends_on: str | None = None

#: The table every column below is added to.
_TABLE = "provider_account_capacity"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("baseline_instance_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("baseline_instance_ids_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("baseline_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "recovery_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column("last_recovery_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("next_recovery_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("canary_lease_ref", sa.String(length=128), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("canary_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("outage_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for name in (
        "last_reminder_at",
        "outage_notified_at",
        "canary_lease_expires_at",
        "canary_lease_ref",
        "next_recovery_attempt_at",
        "last_recovery_attempt_at",
        "recovery_attempts",
        "baseline_observed_at",
        "baseline_instance_ids_hash",
        "baseline_instance_count",
    ):
        op.drop_column(_TABLE, name)
