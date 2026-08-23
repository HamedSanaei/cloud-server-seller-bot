"""add price_book_versions table (M06-001)

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "price_book_versions",
        sa.Column(
            "id",
            postgresql.UUID(),
            primary_key=True,
            server_default=text("uuid_generate_v4()"),
        ),
        sa.Column("book_name", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "rules",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=text("'[]'"),
        ),
        sa.Column("created_at", sa.DateTime(), server_default=text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("book_name", "version", name="uq_price_book_versions_book_version"),
    )
    op.create_index(
        "ix_price_book_versions_book_effective",
        "price_book_versions",
        ["book_name", "effective_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_price_book_versions_book_effective", table_name="price_book_versions")
    op.drop_table("price_book_versions")
