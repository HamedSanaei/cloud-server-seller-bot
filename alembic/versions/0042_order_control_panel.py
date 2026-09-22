"""Provider-order control panel choice (STOREFRONT-REWORK).

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-23

WHAT THIS REVISION DOES
------------------------
Adds NULLable ``provider_orders.control_panel``: the free control-panel
option the customer selected during configuration (NULL = no panel). It is
an order fact recorded before any provider call, alongside ``os_name``.

The worker does not transmit it yet: the ordering POST carries no verified
control-panel field, and sending an unverified field could break order
acceptance. The choice stays visible on the confirmation screen and in the
order row for support and for future provider mapping work.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | None = None
depends_on: str | None = None

TABLE = "provider_orders"
COLUMN = "control_panel"


def _notify(message: str) -> None:
    """Make the revision visible in the deployment log."""
    print(f"[migration {revision}] {message}", flush=True)


def upgrade() -> None:
    columns = {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(TABLE)}
    if COLUMN in columns:
        _notify("provider_orders.control_panel already exists — nothing to do")
        return
    op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=128), nullable=True))
    _notify("added provider_orders.control_panel (NULLable, informational)")


def downgrade() -> None:
    """Refuse: downgrades could orphan recorded customer choices."""
    raise RuntimeError(
        "0042 has no automatic downgrade: dropping control_panel would destroy "
        "recorded order facts. Restore the database from a backup instead."
    )
