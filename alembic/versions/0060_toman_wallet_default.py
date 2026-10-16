"""Use Toman for new wallets without relabeling historical money or invoices.

Revision ID: 0060
Revises: 0059
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Bot onboarding and Wallet already default to IRT. Match direct SQL/ORM
    # inserts too. Existing balances, ledger epochs, contracts and frozen
    # payment credit currencies are deliberately not rewritten by a default.
    op.alter_column(
        "wallets", "currency", existing_type=sa.String(3), nullable=False, server_default="IRT"
    )


def downgrade() -> None:
    # A code rollback must not reintroduce new dollar wallets or reinterpret
    # any monetary history. No automatic currency reversal is safe.
    pass
