"""Classify existing Hetzner hourly offers before storefront navigation starts.

Revision ID: 0053
Revises: 0052

Fresh syncs already persist the same normalized family metadata. This
backfill keeps previously published offers navigable immediately after deploy.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE sellable_offers
            SET technical_metadata = technical_metadata || jsonb_build_object(
                'plan_family', CASE
                    WHEN upper(product_id) LIKE 'CPX%' THEN 'regular_performance'
                    WHEN upper(product_id) LIKE 'CCX%' THEN 'general_purpose'
                    ELSE 'cost_optimized'
                END,
                'plan_family_name', CASE
                    WHEN upper(product_id) LIKE 'CPX%' THEN 'Regular performance / CPX'
                    WHEN upper(product_id) LIKE 'CCX%' THEN 'General purpose / CCX'
                    ELSE 'Cost-optimized / CX & CAX'
                END
            )
            WHERE provider_key = 'hetzner'
              AND billing_model = 'hourly'
              AND upper(product_id) ~ '^(CPX|CCX|CX|CAX)[0-9]+$'
              AND coalesce(technical_metadata->>'plan_family', 'other') = 'other'
            """
        )
    )


def downgrade() -> None:
    raise RuntimeError("0053 cannot safely undo backfill after subsequent provider catalog syncs")
