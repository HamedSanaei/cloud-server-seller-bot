"""Hot-path indexes for sell/accrue/reconcile reads (M16-003)

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-04
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: str | None = "0028b"
branch_labels: str | None = None
depends_on: str | None = None

_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    # Accrual + low-balance passes scan RUNNING servers by watermark.
    ("ix_servers_state_accrued", "servers", ["state", "last_accrued_at"]),
    ("ix_servers_user_state", "servers", ["user_id", "state"]),
    ("ix_servers_provider_state", "servers", ["provider_id", "state"]),
    ("ix_servers_account_state", "servers", ["provider_account_id", "state"]),
    # Wallet/ledger hot reads.
    ("ix_ledger_wallet_created", "ledger", ["wallet_id", "created_at"]),
    ("ix_holds_wallet_status", "holds", ["wallet_id", "status"]),
    # Operation workers poll pending ops per type/server.
    ("ix_operations_status_type", "operations", ["status", "operation_type"]),
    ("ix_operations_server", "operations", ["server_id", "status"]),
    # Outbox publisher polls unprocessed events.
    ("ix_outbox_unprocessed", "outbox", ["processed_at", "created_at"]),
    # Payment reconciliation polls stuck pending sessions.
    ("ix_payment_sessions_status_created", "payment_sessions", ["status", "created_at"]),
    # Accrual business records per server/window.
    ("ix_accrual_periods_server", "accrual_periods", ["server_id", "period_start"]),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({', '.join(columns)})")


def downgrade() -> None:
    for name, _table, _columns in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
