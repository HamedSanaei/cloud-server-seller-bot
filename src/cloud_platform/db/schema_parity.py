"""Physical schema/code parity gate (deploy-time, read-only).

WHY THIS EXISTS
---------------
Revision equality is NOT evidence of a compatible database. Production proved
it: the database reported revision ``0037`` while objects created by migration
``0035`` (``provider_routes``) and the multi-account credential-account columns
were physically ABSENT. A deploy check of ``db_head == image_head`` passes on
that database, and the application then dies at runtime with
``relation "provider_routes" does not exist`` — a customer-visible failure that
no revision number could have predicted.

So the release verifies the PHYSICAL schema its own code queries, after the
migration step and BEFORE api/worker/bot are replaced.

WHAT "COMPATIBLE" MEANS
-----------------------
The authority is the release's own SQLAlchemy metadata (``Base.metadata``): the
tables and columns the code will SELECT/INSERT, plus the uniqueness its upserts
rely on. For every mapped table:

* the table must exist;
* every mapped column must exist;
* every unique constraint / unique index the models declare must be enforced
  on the same column set. Uniqueness is matched by COLUMNS, never by name, so a
  database that spells (or creates) the key differently is not called drift —
  and a superset key counts, because uniqueness on ``(a, b, c)`` implies it on
  ``(a, b)``.

Extra tables, extra columns and non-unique indexes are tolerated: they cannot
break the release.

A healthy database at the release's head satisfies all of this by construction —
``tests/unit/test_schema_code_parity.py`` proves every mapped column is created
by some migration's ``upgrade()`` — so this is a drift detector, not a schema
diff, and it cannot fail a correct deploy.

NOTHING HERE IS PROVIDER-SPECIFIC, AND NOTHING HERE MUTATES ANYTHING
--------------------------------------------------------------------
It reflects the database and reports. It never writes, never migrates, never
stamps a revision, and never prints a connection string, a credential or a
configuration value: only object names are reported.

Usage (inside the release image, where the server configuration is mounted):
    docker compose run --rm --no-deps migrate python -m cloud_platform.db.schema_parity

Exit code 0 = the physical schema supports the release; non-zero = it does not.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Inspector, MetaData

from cloud_platform.db.base import Base

#: The one-line remedy, printed with any drift (read-only guidance).
REPAIR_HINT = (
    "the database has drifted from its recorded revision: apply the release's "
    "migrations (`alembic upgrade head`) — if the revision already matches, a "
    "forward repair migration is what restores the missing objects — then rerun "
    "this gate before starting services"
)

#: Exit codes (documented so the deploy script and operators can rely on them).
EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_UNINSPECTABLE = 2


@dataclass(slots=True)
class RequiredSchema:
    """What the release's code needs (derived from its own models)."""

    columns: dict[str, frozenset[str]]
    unique_keys: dict[str, frozenset[frozenset[str]]]


@dataclass(slots=True)
class ObservedSchema:
    """What the database physically has (from a read-only inspector)."""

    tables: frozenset[str]
    columns: dict[str, frozenset[str]]
    unique_keys: dict[str, frozenset[frozenset[str]]]
    unreadable: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SchemaDrift:
    """The difference that would make the release fail at runtime."""

    missing_tables: list[str]
    missing_columns: list[tuple[str, str]]
    missing_unique_keys: list[tuple[str, frozenset[str]]]
    unreadable_tables: list[tuple[str, str]]
    checked_tables: int

    @property
    def ok(self) -> bool:
        return not (
            self.missing_tables
            or self.missing_columns
            or self.missing_unique_keys
            or self.unreadable_tables
        )


def required_schema(metadata: MetaData | None = None) -> RequiredSchema:
    """Table -> required columns / required uniqueness, from the release's models."""
    resolved = Base.metadata if metadata is None else metadata
    columns: dict[str, frozenset[str]] = {}
    unique_keys: dict[str, frozenset[frozenset[str]]] = {}
    for name, table in resolved.tables.items():
        columns[name] = frozenset(column.name for column in table.columns)
        keys: set[frozenset[str]] = set()
        for constraint in table.constraints:
            if isinstance(constraint, sa.UniqueConstraint) and constraint.columns:
                keys.add(frozenset(column.name for column in constraint.columns))
        for index in table.indexes:
            if index.unique:
                keys.add(frozenset(column.name for column in index.columns))
        unique_keys[name] = frozenset(key for key in keys if key)
    return RequiredSchema(columns=columns, unique_keys=unique_keys)


def observe_schema(inspector: Inspector) -> ObservedSchema:
    """Read the physical schema. Read-only; a table it cannot read is recorded."""
    tables = frozenset(inspector.get_table_names())
    columns: dict[str, frozenset[str]] = {}
    unique_keys: dict[str, frozenset[frozenset[str]]] = {}
    unreadable: dict[str, str] = {}
    for name in sorted(tables):
        try:
            columns[name] = frozenset(column["name"] for column in inspector.get_columns(name))
            keys: set[frozenset[str]] = set()
            for constraint in inspector.get_unique_constraints(name):
                key = frozenset(
                    column for column in (constraint.get("column_names") or ()) if column
                )
                if key:
                    keys.add(key)
            for index in inspector.get_indexes(name):
                if index.get("unique"):
                    key = frozenset(
                        column for column in (index.get("column_names") or ()) if column
                    )
                    if key:
                        keys.add(key)
            unique_keys[name] = frozenset(keys)
        except Exception as exc:
            # Any reflection failure counts as unresolved evidence (see above).
            # Class name only: reflection errors can embed connection details,
            # and this gate must never print anything secret.
            unreadable[name] = type(exc).__name__
            columns.pop(name, None)
            unique_keys.pop(name, None)
    return ObservedSchema(
        tables=tables, columns=columns, unique_keys=unique_keys, unreadable=unreadable
    )


def evaluate(observed: ObservedSchema, required: RequiredSchema | None = None) -> SchemaDrift:
    """Compare what the release needs against what the database physically has."""
    resolved = required_schema() if required is None else required
    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []
    missing_unique_keys: list[tuple[str, frozenset[str]]] = []
    for table in sorted(resolved.columns):
        if table not in observed.tables:
            missing_tables.append(table)
            continue
        if table in observed.unreadable:
            continue  # reported separately, and it fails closed below
        present = observed.columns.get(table, frozenset())
        for column in sorted(resolved.columns[table] - present):
            missing_columns.append((table, column))
        enforced = observed.unique_keys.get(table, frozenset())
        for key in sorted(resolved.unique_keys[table], key=lambda item: sorted(item)):
            # A database key that is a SUPERSET of this one enforces it too.
            if not any(key <= candidate for candidate in enforced):
                missing_unique_keys.append((table, key))
    return SchemaDrift(
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        missing_unique_keys=missing_unique_keys,
        unreadable_tables=sorted(observed.unreadable.items()),
        checked_tables=len(resolved.columns),
    )


def render(drift: SchemaDrift) -> str:
    """A deploy-log report. Object names only - never a value or a secret."""
    if drift.ok:
        return (
            f"[ok  ] physical schema matches the release ({drift.checked_tables} table(s) verified)"
        )
    lines = ["[FAIL] the release's code queries schema this database does not have:"]
    lines.extend(f"  missing table:   {table}" for table in drift.missing_tables)
    lines.extend(f"  missing column:  {table}.{column}" for table, column in drift.missing_columns)
    lines.extend(
        f"  missing uniqueness on {table}({', '.join(sorted(key))}) "
        "- upserts that rely on it will fail"
        for table, key in drift.missing_unique_keys
    )
    lines.extend(
        f"  unreadable table {table} ({error_class}) - compatibility cannot be proven"
        for table, error_class in drift.unreadable_tables
    )
    lines.append(f"  {REPAIR_HINT}")
    return "\n".join(lines)


async def observe_live() -> ObservedSchema:
    """Reflect the configured database with a short-lived, read-only engine."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from cloud_platform.core.config import get_settings

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:

            def _reflect(sync_connection: Any) -> ObservedSchema:
                return observe_schema(sa.inspect(sync_connection))

            return await connection.run_sync(_reflect)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="schema_parity",
        description="Fail when the database lacks schema the release's code queries.",
    )
    parser.parse_args(argv)

    try:
        observed = asyncio.run(observe_live())
    except Exception as exc:
        # An uninspectable database is not a compatible one: fail closed.
        print(
            "[FAIL] cannot inspect the database schema "
            f"({type(exc).__name__}): refusing to treat the database as compatible",
            file=sys.stderr,
        )
        return EXIT_UNINSPECTABLE

    drift = evaluate(observed)
    print(render(drift))
    return EXIT_OK if drift.ok else EXIT_DRIFT


if __name__ == "__main__":
    raise SystemExit(main())
