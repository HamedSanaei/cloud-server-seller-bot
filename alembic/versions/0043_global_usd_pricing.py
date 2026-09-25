"""Global USD storefront pricing and immutable money-side snapshots.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-24

WHAT THIS REVISION DOES
------------------------
Adds the small schema surface needed to keep provider-native cost and the
customer selling side independent:

* ``sellable_offers.pricing_metadata`` is a NOT NULL JSONB object with an
  empty-object server default, so existing rows remain valid while catalog
  pricing audit data has a durable home.
* ``server_price_snapshots.selling_currency`` is nullable.  Rows written
  before the global-USD change fall back to their existing ``currency`` (the
  provider cost currency) when read. ``provider_rate_exact`` preserves a
  sub-cent native Decimal provider rate when available.
* ``accrual_periods.cost_amount`` preserves the exact native major-unit cost;
  ``cost_currency`` and
  ``accrual_periods.selling_currency`` are nullable.  Legacy rows fall back
  to the existing ``currency`` value on both sides.

No historical cost is converted or relabelled.  The migration is guarded by
an inspector so a partially provisioned database can be repaired forward;
it does not use raw SQL.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | None = None
depends_on: str | None = None


def _notify(message: str) -> None:
    print(f"[migration {revision}] {message}", flush=True)


def _column_names(table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table)}


def _existing_column(table: str, name: str) -> dict[str, object] | None:
    for column in sa.inspect(op.get_bind()).get_columns(table):
        if str(column["name"]) == name:
            return column
    return None


def _normalize_default(value: object) -> str:
    """Normalize a catalog server default FOR COMPARISON ONLY.

    PostgreSQL echoes a default back in its canonical form, so the literal this
    migration sets is not the literal it reads back: ``{}`` on a JSONB column
    introspects as ``'{}'::jsonb`` and ``USD`` as ``'USD'::character varying``.
    Comparing those verbatim would re-issue the identical ``SET DEFAULT`` on
    every repair pass.  The cast suffix and the literal quotes are therefore
    dropped here; nothing is ever written from this value, so a wrong
    normalization can only cost a redundant (idempotent) ALTER, never a guessed
    default.  Values that do not look like a quoted literal - for example
    ``nextval('seq'::regclass)`` - are returned unchanged.
    """
    text = str(value or "").strip()
    if text.startswith("'"):
        closing = text.find("'", 1)
        if closing > 0:
            remainder = text[closing + 1 :].strip()
            if not remainder or remainder.startswith("::"):
                return text[1:closing].strip()
    return text.strip("'").strip()


def _type_matches(current_type: object, expected_type: sa.types.TypeEngine[Any]) -> bool:
    """Whether a reflected column type already has the intended shape.

    ``current_type`` comes from ``sqlalchemy.inspect`` on a live database, so
    it is a DIALECT type (``postgresql.VARCHAR``, ``postgresql.TEXT``,
    ``postgresql.JSONB``), not necessarily the generic class declared above.
    Identity/name comparison would report drift for every healthy column and
    re-run ``ALTER COLUMN ... TYPE`` on each pass, so a SUBCLASS check is used:
    dialect subclasses of the expected type match, while a genuinely different
    shape (INTEGER where TEXT is intended) does not.

    ``isinstance`` takes a type or a tuple of types - never an instance - so
    this compares against ``type(expected_type)``.  Feeding the TypeEngine
    instance itself to ``isinstance`` is what crashed the fresh-database
    migration with ``TypeError: isinstance() arg 2 must be a type...``.
    """
    if not isinstance(current_type, type(expected_type)):
        return False
    if isinstance(expected_type, sa.String) and expected_type.length is not None:
        # The intended width is part of the shape: VARCHAR(3) and VARCHAR(255)
        # must stay distinguishable so drift is repaired, not masked.
        return getattr(current_type, "length", None) == expected_type.length
    return True


def _repair_column_shape(
    table: str,
    name: str,
    *,
    type_: sa.types.TypeEngine[Any],
    nullable: bool,
    server_default: object | None,
) -> None:
    """Repair drift in a pre-existing pricing column without guessing money.

    ``type_`` is always a SQLAlchemy type INSTANCE (``sa.Text()``,
    ``sa.String(length=3)``, ``JSONB()``); never a type CLASS, which would
    change the meaning of the comparison below.
    """
    current = _existing_column(table, name)
    if current is None:
        return
    current_type = current.get("type")
    if current_type is not None and not _type_matches(current_type, type_):
        kwargs: dict[str, object] = {
            "type_": type_,
            "existing_type": current_type,
            "existing_nullable": bool(current.get("nullable", True)),
        }
        if str(current.get("default") or "").strip():
            kwargs["existing_server_default"] = current.get("default")
        if table == "server_price_snapshots" and name == "provider_rate_exact":
            # Textual widening only: the stored digits are preserved verbatim.
            kwargs["postgresql_using"] = f"{name}::text"
        op.alter_column(table, name, **kwargs)
    if bool(current.get("nullable", True)) != nullable:
        op.alter_column(
            table,
            name,
            nullable=nullable,
            existing_type=type_,
            existing_server_default=current.get("default"),
        )
    current_default = current.get("default")
    if server_default is not None:
        if _normalize_default(current_default) != _normalize_default(server_default):
            op.alter_column(
                table,
                name,
                server_default=server_default,
                existing_type=type_,
                existing_nullable=nullable,
            )
    elif current_default is not None:
        op.alter_column(
            table,
            name,
            server_default=None,
            existing_type=type_,
            existing_nullable=nullable,
        )


def upgrade() -> None:
    """Add every pricing column behind an inspector guard.

    Each statement is deliberately written as a literal
    ``op.add_column(<table>, sa.Column(<name>, ...))`` call: the repository's
    structural schema-parity gate reads migrations with an AST walk and can
    only prove that a model column is created by an upgrade side when both the
    table and the column name are literal here.
    """
    added: list[str] = []

    if "pricing_metadata" not in _column_names("sellable_offers"):
        op.add_column(
            "sellable_offers",
            sa.Column("pricing_metadata", JSONB(), nullable=False, server_default="{}"),
        )
        added.append("sellable_offers.pricing_metadata")

    if "selling_currency" not in _column_names("server_price_snapshots"):
        op.add_column(
            "server_price_snapshots",
            sa.Column("selling_currency", sa.String(length=3), nullable=True),
        )
        added.append("server_price_snapshots.selling_currency")

    if "provider_rate_exact" not in _column_names("server_price_snapshots"):
        op.add_column(
            "server_price_snapshots",
            sa.Column("provider_rate_exact", sa.Text(), nullable=True),
        )
        added.append("server_price_snapshots.provider_rate_exact")

    if "pricing_metadata" not in _column_names("server_price_snapshots"):
        op.add_column(
            "server_price_snapshots",
            sa.Column("pricing_metadata", JSONB(), nullable=False, server_default="{}"),
        )
        added.append("server_price_snapshots.pricing_metadata")

    if "offer_fingerprint" not in _column_names("server_price_snapshots"):
        op.add_column(
            "server_price_snapshots",
            sa.Column("offer_fingerprint", JSONB(), nullable=True),
        )
        added.append("server_price_snapshots.offer_fingerprint")

    if "image_id" not in _column_names("servers"):
        op.add_column("servers", sa.Column("image_id", sa.String(), nullable=True))
        added.append("servers.image_id")

    if "offer_fingerprint" not in _column_names("servers"):
        op.add_column("servers", sa.Column("offer_fingerprint", JSONB(), nullable=True))
        added.append("servers.offer_fingerprint")

    if "cost_currency" not in _column_names("accrual_periods"):
        op.add_column(
            "accrual_periods",
            sa.Column("cost_currency", sa.String(length=3), nullable=True),
        )
        added.append("accrual_periods.cost_currency")

    if "selling_currency" not in _column_names("accrual_periods"):
        op.add_column(
            "accrual_periods",
            sa.Column("selling_currency", sa.String(length=3), nullable=True),
        )
        added.append("accrual_periods.selling_currency")

    if "cost_amount" not in _column_names("accrual_periods"):
        op.add_column("accrual_periods", sa.Column("cost_amount", sa.Text(), nullable=True))
        added.append("accrual_periods.cost_amount")

    if "rule_key" not in _column_names("accrual_periods"):
        op.add_column("accrual_periods", sa.Column("rule_key", sa.String(), nullable=True))
        added.append("accrual_periods.rule_key")

    if "provider_monthly_rate_exact" not in _column_names("provider_orders"):
        op.add_column(
            "provider_orders",
            sa.Column("provider_monthly_rate_exact", sa.Text(), nullable=True),
        )
        added.append("provider_orders.provider_monthly_rate_exact")

    if "pricing_metadata" not in _column_names("provider_orders"):
        op.add_column(
            "provider_orders",
            sa.Column("pricing_metadata", JSONB(), nullable=False, server_default="{}"),
        )
        added.append("provider_orders.pricing_metadata")

    if added:
        _notify("added " + ", ".join(added))
    else:
        _notify("global USD pricing columns already present — nothing to do")

    # Repair shape for columns that a partially applied build may already have
    # created.  Every exact-money field is text; JSON audit objects are NOT
    # NULL; and the currency fields retain their intended defaults.  The
    # expected types are INSTANCES (``JSONB()``, not the ``JSONB`` class): the
    # comparison in ``_repair_column_shape`` is an ``isinstance`` check, which
    # requires a type, and the reflected type is always an instance.
    for table, column, type_, nullable, default in (
        ("sellable_offers", "pricing_metadata", JSONB(), False, "{}"),
        ("server_price_snapshots", "provider_rate_exact", sa.Text(), True, None),
        ("server_price_snapshots", "pricing_metadata", JSONB(), False, "{}"),
        ("server_price_snapshots", "selling_currency", sa.String(length=3), True, None),
        ("server_price_snapshots", "offer_fingerprint", JSONB(), True, None),
        ("servers", "image_id", sa.String(), True, None),
        ("servers", "offer_fingerprint", JSONB(), True, None),
        ("accrual_periods", "cost_currency", sa.String(length=3), True, None),
        ("accrual_periods", "selling_currency", sa.String(length=3), True, None),
        ("accrual_periods", "cost_amount", sa.Text(), True, None),
        ("accrual_periods", "rule_key", sa.String(), True, None),
        ("provider_orders", "provider_monthly_rate_exact", sa.Text(), True, None),
        ("provider_orders", "pricing_metadata", JSONB(), False, "{}"),
    ):
        _repair_column_shape(
            table,
            column,
            type_=type_,
            nullable=nullable,
            server_default=default,
        )

    # Fill only the non-financial JSON audit projection.  A missing exact
    # provider rate is intentionally left NULL and marked for quarantine below;
    # inventing a rounded value would be a financial corruption.
    bind = op.get_bind()
    for table, column in (
        ("sellable_offers", "pricing_metadata"),
        ("server_price_snapshots", "pricing_metadata"),
        ("provider_orders", "pricing_metadata"),
    ):
        if _existing_column(table, column) is not None:
            metadata_table = sa.table(table, sa.column(column, JSONB))
            bind.execute(
                sa.update(metadata_table)
                .where(metadata_table.c[column].is_(None))
                .values({column: {}})
            )

    # Mark legacy exact-rate gaps explicitly.  Billing and the worker already
    # fail closed on NULL; this durable marker makes the quarantine auditable.
    for table, column, metadata_column in (
        ("server_price_snapshots", "provider_rate_exact", "pricing_metadata"),
        ("provider_orders", "provider_monthly_rate_exact", "pricing_metadata"),
    ):
        if (
            _existing_column(table, column) is not None
            and _existing_column(table, metadata_column) is not None
        ):
            metadata = sa.table(
                table,
                sa.column(column),
                sa.column(metadata_column, JSONB),
            )
            # JSONB concatenation preserves all pre-existing audit fields.
            bind.execute(
                sa.update(metadata)
                .where(metadata.c[column].is_(None))
                .values(
                    {
                        metadata_column: metadata.c[metadata_column].concat(
                            {"legacy_exact_missing": True}
                        )
                    }
                )
            )

    # New native-cost rows must carry an explicit provider currency.  Do not
    # retain the historical EUR server default, which could silently relabel a
    # corrupt/direct write as a native EUR cost.
    for table, column in (
        ("sellable_offers", "provider_cost_currency"),
        ("servers", "currency"),
    ):
        if column in _column_names(table):
            op.alter_column(
                table,
                column,
                server_default=None,
                existing_type=sa.String(length=3),
                existing_nullable=False,
            )

    # Wallet defaults are a creation policy only; existing wallet currencies
    # are retained as their original native balance labels.
    if "currency" in _column_names("wallets"):
        op.alter_column(
            "wallets",
            "currency",
            server_default="USD",
            existing_type=sa.String(length=3),
            existing_nullable=False,
        )


def downgrade() -> None:
    """Refuse destructive downgrade: these columns hold immutable audit facts."""
    raise RuntimeError(
        "0043 has no automatic downgrade: dropping pricing metadata, selling/cost "
        "currencies, or exact provider cost columns would destroy immutable pricing and "
        "billing audit data. Restore the database from a backup instead."
    )
