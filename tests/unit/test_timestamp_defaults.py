"""Regression cover for the production ``CURRENT_TIMESTAMP`` binding failure.

Production offer upserts failed with::

    invalid input for query argument: 'CURRENT_TIMESTAMP'
    expected datetime.date/datetime, got str

The cause was a column declared as ``onupdate="CURRENT_TIMESTAMP"``. SQLAlchemy
classifies a plain string as a SCALAR default — a Python *value* — so it renders
the column as a bind parameter (``updated_at=%(updated_at)s``) and hands asyncpg
the string where a ``datetime`` was expected. ``sa.text("CURRENT_TIMESTAMP")`` is
a SQL *expression*: it is rendered inline (``updated_at=CURRENT_TIMESTAMP``) and
never bound.

These tests cover the whole repository, not just the offer row that surfaced the
bug: the pattern is declarative and would be reintroduced by any model. They
assert on a COMPILED UPDATE — the statement the driver actually receives — and
they prove the detector itself fails on the unsafe spelling, so the guard cannot
pass vacuously.
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from cloud_platform.db.base import Base, SellableOffer

#: The literal that must never reach a DateTime parameter.
UNSAFE = "CURRENT_TIMESTAMP"

#: A column rendered as a bind parameter in a SET clause (``name=%(name)s``).
_BOUND_IN_SET = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*%\(")


def _compiled_update(table: sa.Table) -> sa.sql.Compiled:
    """Compile a real UPDATE for ``table``.

    Every column carrying ``onupdate`` therefore lands in the SET clause, which
    is exactly what the driver receives in production.
    """
    target = next(column for column in table.columns if not column.primary_key)
    return sa.update(table).values({target.name: None}).compile(dialect=postgresql.dialect())


def _bound_in_set(compiled: sa.sql.Compiled) -> set[str]:
    """Column names the statement sends as bind parameters."""
    return set(_BOUND_IN_SET.findall(" ".join(str(compiled).split())))


def _is_value_default(column: sa.Column) -> bool:
    """Whether ``onupdate`` is a scalar VALUE rather than a SQL expression.

    A scalar default is bound as a parameter (`is_scalar`); a SQL expression or
    a Python callable is not, and both are safe for a DateTime column.
    """
    return bool(getattr(column.onupdate, "is_scalar", False))


def _onupdate_columns() -> list[tuple[str, sa.Column]]:
    return [
        (table.name, column)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.onupdate is not None
    ]


class TestTimestampDefaults:
    """No model may route a timestamp through a string bind parameter."""

    def test_the_suite_actually_covers_the_models(self) -> None:
        """A guard that silently covered nothing would be worse than none."""
        covered = _onupdate_columns()
        assert len(covered) >= 5, f"expected many timestamped tables, found {len(covered)}"
        assert any(column.table.name == SellableOffer.__table__.name for _, column in covered)

    def test_no_onupdate_is_a_scalar_value(self) -> None:
        """A bare-string ``onupdate`` IS the production bug."""
        offenders = [
            f"{table}.{column.name}={column.onupdate!r}"
            for table, column in _onupdate_columns()
            if _is_value_default(column)
        ]
        assert offenders == [], (
            "columns declare a scalar onupdate, which SQLAlchemy binds as a VALUE "
            "(asyncpg then rejects 'CURRENT_TIMESTAMP' for a DateTime column): "
            + ", ".join(offenders)
        )

    def test_no_compiled_update_binds_its_timestamp(self) -> None:
        """Repository-wide: every UPDATE sends the timestamp as SQL, not a value."""
        for table_name, column in _onupdate_columns():
            compiled = _compiled_update(Base.metadata.tables[table_name])
            assert column.name not in _bound_in_set(compiled), (
                f"{table_name}: UPDATE renders {column.name} as a bind parameter: "
                f"{' '.join(str(compiled).split())}"
            )

    def test_sellable_offer_update_is_server_side(self) -> None:
        """The model from the incident, asserted directly on its UPDATE."""
        table = SellableOffer.__table__
        sql = " ".join(str(_compiled_update(table)).split())
        assert f"updated_at={UNSAFE}" in sql
        assert "updated_at=%(" not in sql
        # The stored server default is untouched: new rows still take a database
        # timestamp, so the fix is confined to the UPDATE path.
        assert table.c.updated_at.server_default is not None
        assert table.c.created_at.server_default is not None

    def test_second_representative_model_update_is_server_side(self) -> None:
        """The same assertion for another timestamped model (the pattern is shared)."""
        table = next(
            table
            for table in Base.metadata.tables.values()
            if table.name != SellableOffer.__table__.name
            and any(column.onupdate is not None for column in table.columns)
        )
        sql = " ".join(str(_compiled_update(table)).split())
        assert f"updated_at={UNSAFE}" in sql
        assert "updated_at=%(" not in sql

    def test_detector_catches_the_unsafe_spelling(self) -> None:
        """Prove the guard FAILS on the production spelling.

        Without this, a mistake in the detector would let the whole module pass
        while the bug came back.
        """
        unsafe = sa.Table(
            "regression_probe_unsafe",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime, server_default=UNSAFE),
            sa.Column("updated_at", sa.DateTime, server_default=UNSAFE, onupdate=UNSAFE),
        )
        unsafe_sql = " ".join(str(_compiled_update(unsafe)).split())
        assert _is_value_default(unsafe.c.updated_at) is True
        assert "updated_at=%(" in unsafe_sql
        assert f"updated_at={UNSAFE}" not in unsafe_sql

        safe = sa.Table(
            "regression_probe_safe",
            sa.MetaData(),
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("created_at", sa.DateTime, server_default=UNSAFE),
            sa.Column(
                "updated_at",
                sa.DateTime,
                server_default=UNSAFE,
                onupdate=sa.text(UNSAFE),
            ),
        )
        safe_sql = " ".join(str(_compiled_update(safe)).split())
        assert _is_value_default(safe.c.updated_at) is False
        assert f"updated_at={UNSAFE}" in safe_sql
        assert "updated_at=%(" not in safe_sql
