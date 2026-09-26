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
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("baseline_instance_count", sa.Column("baseline_instance_count", sa.Integer(), nullable=True)),
    (
        "baseline_instance_ids_hash",
        sa.Column("baseline_instance_ids_hash", sa.String(length=64), nullable=True),
    ),
    (
        "baseline_observed_at",
        sa.Column("baseline_observed_at", sa.DateTime(timezone=True), nullable=True),
    ),
    (
        "recovery_attempts",
        sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    ),
    (
        "last_recovery_attempt_at",
        sa.Column("last_recovery_attempt_at", sa.DateTime(timezone=True), nullable=True),
    ),
    (
        "next_recovery_attempt_at",
        sa.Column("next_recovery_attempt_at", sa.DateTime(timezone=True), nullable=True),
    ),
    (
        "canary_lease_ref",
        sa.Column("canary_lease_ref", sa.String(length=128), nullable=True),
    ),
    (
        "canary_lease_expires_at",
        sa.Column("canary_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    ),
    (
        "outage_notified_at",
        sa.Column("outage_notified_at", sa.DateTime(timezone=True), nullable=True),
    ),
    (
        "last_reminder_at",
        sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True),
    ),
)


def upgrade() -> None:
    for _name, column in _COLUMNS:
        op.add_column("provider_account_capacity", column)


def downgrade() -> None:
    for name, _column in reversed(_COLUMNS):
        op.drop_column("provider_account_capacity", name)
