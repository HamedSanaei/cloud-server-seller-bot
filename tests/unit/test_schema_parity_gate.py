"""Physical schema gate: revision equality is NOT compatibility.

Production evidence (the incident these tests pin down), read from the
application database itself:

    alembic_version = 0037                 # newer than 0035
    migration 0036's FX columns            PRESENT
    migration 0037's sellable_offers col   PRESENT
    provider_routes                        MISSING   <- created by 0035
    provider_orders.credential_account_id  MISSING   <- created by 0035
    servers.credential_account_id          MISSING   <- created by 0035

A deploy gate of ``db_head == image_head`` PASSES on that database, because the
revision really is 0037. The application then fails at runtime with
``relation "provider_routes" does not exist``, and every offer upsert fails.

So the release checks the PHYSICAL schema its own code queries
(``cloud_platform.db.schema_parity``). These tests reproduce the production
shape exactly and prove:

  * that shape is REJECTED, naming the three missing objects;
  * the same shape AFTER the forward repair (migration 0038) is ACCEPTED;
  * the two shapes are indistinguishable by revision, so the physical gate is
    the only thing that can tell them apart.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from cloud_platform.db import schema_parity as sp
from scripts.post_deploy_smoke import repo_head_revision

REPO_ROOT = Path(__file__).resolve().parents[2]
REPAIR_MIGRATION = REPO_ROOT / "alembic" / "versions" / "0038_repair_multi_account_schema_drift.py"
MODULE = REPO_ROOT / "src" / "cloud_platform" / "db" / "schema_parity.py"

#: The three objects production was missing (all declared by migration 0035).
DRIFTED_TABLE = "provider_routes"
DRIFTED_COLUMNS = (
    ("provider_orders", "credential_account_id"),
    ("servers", "credential_account_id"),
)


def _at_head(
    *,
    drop_tables: frozenset[str] = frozenset(),
    drop_columns: frozenset[tuple[str, str]] = frozenset(),
    drop_unique: frozenset[tuple[str, frozenset[str]]] = frozenset(),
    rename_unique: bool = False,
) -> sp.ObservedSchema:
    """What a database at the release's head physically looks like.

    Built from the models, which is not an assumption: ``test_schema_code_parity``
    proves every mapped table and column is created by some migration's
    ``upgrade()``, so a healthy at-head database has exactly these objects.
    ``rename_unique`` shows the gate does not care what a key is CALLED.
    """
    required = sp.required_schema()
    columns: dict[str, frozenset[str]] = {}
    unique_keys: dict[str, frozenset[frozenset[str]]] = {}
    for table, table_columns in required.columns.items():
        if table in drop_tables:
            continue
        kept = frozenset(column for column in table_columns if (table, column) not in drop_columns)
        columns[table] = kept
        keys = {key for key in required.unique_keys[table] if (table, key) not in drop_unique}
        if rename_unique:
            # Same columns, arbitrary names: uniqueness is matched by COLUMNS.
            keys = {frozenset(key) for key in keys}
        unique_keys[table] = frozenset(keys)
    return sp.ObservedSchema(
        tables=frozenset(columns),
        columns=columns,
        unique_keys=unique_keys,
    )


def _production_shape() -> sp.ObservedSchema:
    """The confirmed production schema, reproduced object for object.

    The revision is 0037 and the later migrations' effects are present, so the
    ONLY difference from a healthy database is the three drift objects.
    """
    return _at_head(drop_tables=frozenset({DRIFTED_TABLE}), drop_columns=frozenset(DRIFTED_COLUMNS))


def _after_repair() -> sp.ObservedSchema:
    """The same database once migration 0038 has restored the missing objects."""
    return _at_head()


class TestProductionDriftIsRejected:
    """The exact production shape must fail the gate, and name what is missing."""

    def test_the_production_shape_is_rejected(self) -> None:
        drift = sp.evaluate(_production_shape())
        assert not drift.ok
        assert drift.missing_tables == [DRIFTED_TABLE]
        assert drift.missing_columns == list(DRIFTED_COLUMNS)

    def test_the_report_names_the_missing_objects_and_the_remedy(self) -> None:
        report = sp.render(sp.evaluate(_production_shape()))
        assert "provider_routes" in report
        assert "provider_orders.credential_account_id" in report
        assert "servers.credential_account_id" in report
        assert "alembic upgrade head" in report
        # Names only: the gate must never print a value it read from the database.
        assert "postgres" not in report.lower().replace("provider_orders", "")

    def test_after_the_forward_repair_the_gate_passes(self) -> None:
        drift = sp.evaluate(_after_repair())
        assert drift.ok, sp.render(drift)
        assert drift.checked_tables == len(sp.required_schema().columns)

    def test_revision_alone_cannot_distinguish_the_two_shapes(self) -> None:
        """Both are revision 0037 (and 0036/0037 effects are present).

        The gate must therefore not consult ``alembic_version`` at all - a
        stamped revision is not evidence of schema.
        """
        required = sp.required_schema()
        assert "alembic_version" not in required.columns
        assert "alembic_version" not in (required.columns.get(DRIFTED_TABLE) or frozenset())
        # The later migrations' effects are present, so the ONLY difference
        # between the shapes is the three drift objects.
        production = _production_shape()
        healthy = _at_head()
        drifted = {table for table, _column in DRIFTED_COLUMNS} | {DRIFTED_TABLE}
        assert production.tables == healthy.tables - {DRIFTED_TABLE}
        for table, columns in production.columns.items():
            if table in drifted:
                continue
            assert columns == healthy.columns[table], table
            assert production.unique_keys[table] == healthy.unique_keys[table]
        for table, column in DRIFTED_COLUMNS:
            assert healthy.columns[table] - production.columns[table] == {column}

    def test_the_gate_only_passes_once_the_repair_revision_ships(self) -> None:
        """The gate is meaningful only with 0038 in the release it guards.

        Deliberately pinned to the CURRENT head: every new migration must update
        this number, which is the moment to confirm that what it ships is also
        what ``required_schema()`` (the release's own metadata) now demands —
        0047 does, with the automatic canary-recovery columns on
        ``provider_account_capacity`` (baseline census, attempt schedule,
        durable canary lease, outage bookkeeping).
        """
        assert repo_head_revision() == "0047"
        assert REPAIR_MIGRATION.is_file()
        source = REPAIR_MIGRATION.read_text(encoding="utf-8")
        assert DRIFTED_TABLE in source
        assert "credential_account_id" in source
        for table, _column in DRIFTED_COLUMNS:
            assert table in source

    def test_the_missing_uniqueness_would_be_reported_too(self) -> None:
        """0035 also creates the (account, location) uniqueness the upserts need."""
        required = sp.required_schema()
        key = next(iter(required.unique_keys[DRIFTED_TABLE]))
        observed = _at_head(drop_unique=frozenset({(DRIFTED_TABLE, key)}))
        drift = sp.evaluate(observed)
        assert drift.missing_unique_keys == [(DRIFTED_TABLE, key)]


class TestDriftDetection:
    """What counts as drift, and - just as important - what does not."""

    def test_extra_tables_columns_and_non_unique_indexes_are_tolerated(self) -> None:
        required = sp.required_schema()
        table = sorted(required.columns)[0]
        observed = _at_head()
        observed.tables = observed.tables | {"an_extension_table"}
        observed.columns["an_extension_table"] = frozenset({"id"})
        observed.columns[table] = observed.columns[table] | {"operator_note"}
        assert sp.evaluate(observed).ok

    def test_uniqueness_is_matched_by_columns_not_by_name(self) -> None:
        # A database that spells the key differently still enforces it.
        assert sp.evaluate(_at_head(rename_unique=True)).ok

    def test_a_superset_key_enforces_the_required_one(self) -> None:
        required = sp.required_schema()
        table = DRIFTED_TABLE
        key = next(iter(required.unique_keys[table]))
        observed = _at_head(drop_unique=frozenset({(table, key)}))
        observed.unique_keys[table] = frozenset({key | {"account_state"}})
        assert sp.evaluate(observed).ok

    def test_a_table_that_exists_without_its_columns_is_drift(self) -> None:
        """A partially applied 0035: the table is there, its columns are not."""
        observed = _at_head()
        observed.columns[DRIFTED_TABLE] = frozenset({"id"})
        drift = sp.evaluate(observed)
        assert not drift.ok
        assert drift.missing_tables == []
        missing = {column for table, column in drift.missing_columns if table == DRIFTED_TABLE}
        assert "provider_key" in missing and "credential_account_id" in missing

    def test_an_unreadable_table_fails_closed_with_the_exception_class_only(self) -> None:
        observed = _at_head()
        observed.unreadable = {DRIFTED_TABLE: "OperationalError"}
        observed.columns.pop(DRIFTED_TABLE)
        observed.unique_keys.pop(DRIFTED_TABLE)
        drift = sp.evaluate(observed)
        assert not drift.ok
        assert drift.unreadable_tables == [(DRIFTED_TABLE, "OperationalError")]
        report = sp.render(drift)
        assert "OperationalError" in report
        # Never the message: reflection errors can embed connection details.
        assert "password" not in report.lower()

    def test_a_missing_table_is_reported_once_not_per_column(self) -> None:
        drift = sp.evaluate(_production_shape())
        assert drift.missing_tables == [DRIFTED_TABLE]
        assert all(table != DRIFTED_TABLE for table, _ in drift.missing_columns)


class TestGateEntryPoint:
    """Exit codes the deploy gate branches on."""

    def test_production_shape_exits_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _observe() -> sp.ObservedSchema:
            return _production_shape()

        monkeypatch.setattr(sp, "observe_live", _observe)
        assert sp.main([]) == sp.EXIT_DRIFT

    def test_head_shape_exits_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def _observe() -> sp.ObservedSchema:
            return _after_repair()

        monkeypatch.setattr(sp, "observe_live", _observe)
        assert sp.main([]) == sp.EXIT_OK
        assert "matches the release" in capsys.readouterr().out

    def test_an_uninspectable_database_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _observe() -> sp.ObservedSchema:
            raise RuntimeError("database connection refused")

        monkeypatch.setattr(sp, "observe_live", _observe)
        assert sp.main([]) == sp.EXIT_UNINSPECTABLE

    def test_the_exit_codes_are_distinct(self) -> None:
        assert len({sp.EXIT_OK, sp.EXIT_DRIFT, sp.EXIT_UNINSPECTABLE}) == 3


class TestTheGateIsReadOnly:
    """A deploy gate that could change the database would be a new risk."""

    def test_the_module_imports_no_migration_tooling(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "alembic" not in imported

    def test_the_module_never_executes_ddl_or_dml(self) -> None:
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        mutating = {
            "execute",
            "create_all",
            "drop_all",
            "create_table",
            "add_column",
            "drop_table",
            "drop_column",
        }
        offenders = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in mutating
        ]
        assert offenders == []

    def test_the_module_reads_only_the_schema_and_prints_object_names(self) -> None:
        source = MODULE.read_text(encoding="utf-8")
        # It reflects: get_columns/get_table_names/get_unique_constraints/get_indexes.
        assert "inspector.get_table_names()" in source
        assert "database_url" in source  # reads the configured database, nothing else
        assert "os.environ" not in source
        assert "configuration.toml" not in source
        # The report formats object names, never values it read from the database.
        report_tokens = [
            token
            for token in ("{table}", "{column}", "{', '.join(sorted(key))}")
            if token in source
        ]
        assert report_tokens, "the report should render object names"
