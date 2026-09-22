"""Schema/code parity: shipped code must ship the schema it queries.

Production evidence (the incident this pins down): the application queried
``provider_routes`` and reported ``relation "provider_routes" does not exist``.
The table is created by migration ``0035``, which exists in this repository and
must therefore be reachable from ``alembic upgrade head`` — a deployment whose
database never reached 0035 could not have served the routing code.

These tests are structural, not behavioural: they make "a model without a
migration" and "a duplicate/absent provider_routes migration" impossible to
merge, and they prove ``0035`` sits on the chain that ``upgrade head`` walks.
The deploy-side enforcement of "database == image head" lives in
``scripts/deploy-production.sh`` and ``tests/unit/test_deploy_production.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from cloud_platform.db.base import Base

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = REPO_ROOT / "alembic" / "versions"

#: The revision that owns ``provider_routes`` (see the incident above).
PROVIDER_ROUTES_REVISION = "0035"

#: The forward REPAIR revision. It re-declares the same objects behind inspector
#: guards so a drifted database can be healed forward, so it is expected to name
#: tables another revision already owns — but only this one may.
REPAIR_REVISION = "0038"


def _revision(path: Path) -> tuple[str, str | None]:
    """Read ``revision`` / ``down_revision`` from a migration without importing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    revision: str | None = None
    down: str | None = None
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else list(node.targets)
        names = {target.id for target in targets if isinstance(target, ast.Name)}
        value = ast.literal_eval(node.value) if node.value is not None else None
        if "revision" in names:
            revision = str(value)
        elif "down_revision" in names:
            down = None if value is None else str(value)
    assert revision is not None, f"{path.name} has no revision identifier"
    return revision, down


def _migration_files() -> list[Path]:
    return sorted(VERSIONS.glob("[0-9]*.py"))


def _migrations() -> dict[str, tuple[str | None, Path]]:
    migrations: dict[str, tuple[str | None, Path]] = {}
    for path in _migration_files():
        revision, down = _revision(path)
        assert revision not in migrations, f"duplicate revision {revision}"
        migrations[revision] = (down, path)
    return migrations


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "value"`` pairs, so a lifted name still resolves."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and isinstance(node.value.value, str):
                    constants[target.id] = node.value.value
    return constants


def _created_tables(path: Path) -> set[str]:
    """Table names this migration creates (AST, so no import side effects)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    constants = _string_constants(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attribute = func.attr if isinstance(func, ast.Attribute) else ""
        if attribute != "create_table" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
        elif isinstance(first, ast.Name) and first.id in constants:
            names.add(constants[first.id])
    return names


class TestMigrationGraph:
    def test_there_is_exactly_one_head(self) -> None:
        revisions = _migrations()
        parents = {down for down, _ in revisions.values() if down}
        heads = sorted(set(revisions) - parents)
        assert len(heads) == 1, f"expected a single head, got {heads}"
        assert heads[0] == max(revisions), heads

    def test_the_chain_is_linear_and_complete(self) -> None:
        revisions = _migrations()
        assert len(revisions) == len(_migration_files())
        for _revision_id, (down, path) in revisions.items():
            if down is not None:
                assert down in revisions, f"{path.name} points at missing {down}"

    def test_the_provider_routes_migration_is_on_the_upgrade_path(self) -> None:
        """`alembic upgrade head` must APPLY the routing table."""
        revisions = _migrations()
        head = max(revisions)
        walked: list[str] = []
        current: str | None = head
        while current is not None:
            walked.append(current)
            current = revisions[current][0]
        assert PROVIDER_ROUTES_REVISION in walked
        assert walked[-1] == min(revisions), "the chain does not start at revision 0001"

    def test_provider_routes_is_created_by_the_owner_and_the_repair(self) -> None:
        revisions = _migrations()
        owners = sorted(
            revision
            for revision, (_, path) in revisions.items()
            if "provider_routes" in _created_tables(path)
        )
        # Exactly one ORIGINAL owner, plus the guarded repair revision. Any other
        # creator would be a duplicate migration — see test_schema_code_parity's
        # sibling assertions and tests/unit/test_repair_migration.py, which pins
        # that the repair creates only what an inspector says is missing.
        assert owners == sorted([PROVIDER_ROUTES_REVISION, REPAIR_REVISION]), owners


def _upgrade_side(path: Path) -> ast.Module:
    """The migration minus its ``downgrade``: what ``upgrade head`` APPLIES.

    Module-level helpers count (a revision may create its objects inside one),
    but a downgrade body does not: it must never make a column look created.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.FunctionDef) and node.name == "downgrade")
    ]
    return tree


def _created_columns(path: Path) -> dict[str, set[str]]:
    """table -> column names this migration's upgrade side creates."""
    constants = _string_constants(_upgrade_side(path))
    created: dict[str, set[str]] = {}

    def _name(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in constants:
            return constants[node.id]
        return None

    for node in ast.walk(_upgrade_side(path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        table = _name(node.args[0]) if node.args else None
        if table is None:
            continue
        if node.func.attr == "create_table":
            for argument in node.args[1:]:
                if isinstance(argument, ast.Call):
                    column = _name(argument.args[0]) if argument.args else None
                    if column:
                        created.setdefault(table, set()).add(column)
        elif node.func.attr == "add_column" and len(node.args) > 1:
            column_call = node.args[1]
            if isinstance(column_call, ast.Call) and column_call.args:
                column = _name(column_call.args[0])
                if column:
                    created.setdefault(table, set()).add(column)
    return created


def _dropped_on_upgrade(path: Path) -> tuple[set[str], set[tuple[str, str]]]:
    """Tables/columns the UPGRADE side drops (should be none)."""
    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    for node in ast.walk(_upgrade_side(path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            name = node.args[0].value
            if not isinstance(name, str):
                continue
            if node.func.attr == "drop_table":
                tables.add(name)
            elif (
                node.func.attr == "drop_column"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                columns.add((name, node.args[1].value))
    return tables, columns


class TestModelColumnParity:
    """The invariant that makes the deploy-time physical-schema gate safe.

    ``cloud_platform.db.schema_parity`` fails a deploy when the database lacks
    a table or column the release's models declare. That is only a drift
    detector (and not a schema diff that could block correct deploys) because a
    healthy database at the release's head necessarily HAS those objects: every
    mapped table and column is created by some migration's upgrade side, and no
    upgrade side drops schema the models still use.
    """

    def test_every_mapped_column_is_created_by_a_migration_upgrade(self) -> None:
        created: dict[str, set[str]] = {}
        for _revision_id, (_down, path) in _migrations().items():
            for table, columns in _created_columns(path).items():
                created.setdefault(table, set()).update(columns)
        gaps: dict[str, list[str]] = {}
        for table, model in Base.metadata.tables.items():
            have = created.get(table, set())
            missing = sorted(column.name for column in model.columns if column.name not in have)
            if missing:
                gaps[table] = missing
        assert gaps == {}, (
            "model columns no migration creates: "
            f"{gaps} - the physical-schema deploy gate would block every deploy"
        )

    def test_no_upgrade_side_drops_schema_the_models_still_use(self) -> None:
        dropped_tables: set[str] = set()
        dropped_columns: set[tuple[str, str]] = set()
        for _revision_id, (_down, path) in _migrations().items():
            tables, columns = _dropped_on_upgrade(path)
            dropped_tables |= tables
            dropped_columns |= columns
        used_tables = set(Base.metadata.tables)
        used_columns = {
            (table, column.name)
            for table, model in Base.metadata.tables.items()
            for column in model.columns
        }
        assert dropped_tables & used_tables == set()
        assert dropped_columns & used_columns == set()


class TestModelMigrationParity:
    def test_every_model_table_is_created_by_exactly_one_migration(self) -> None:
        """A model without a migration is code the database cannot support."""
        owners: dict[str, list[str]] = {}
        for revision, (_, path) in _migrations().items():
            for table in _created_tables(path):
                owners.setdefault(table, []).append(revision)
        missing = sorted(table for table in Base.metadata.tables if table not in owners)
        assert missing == [], f"model tables with no migration: {missing}"
        # The REPAIR revision re-declares objects it may have to recreate, so it
        # is allowed to share a table with its original owner. Anything else is a
        # duplicate schema definition.
        duplicated = {
            table: [r for r in revisions if r != REPAIR_REVISION]
            for table, revisions in owners.items()
            if len([r for r in revisions if r != REPAIR_REVISION]) > 1
        }
        assert duplicated == {}, f"tables created by several migrations: {duplicated}"

    def test_no_migration_creates_a_table_without_a_model(self) -> None:
        owners: dict[str, list[str]] = {}
        for revision, (_, path) in _migrations().items():
            for table in _created_tables(path):
                owners.setdefault(table, []).append(revision)
        orphans = sorted(table for table in owners if table not in Base.metadata.tables)
        # ``alembic_version`` is alembic's own bookkeeping table.
        assert orphans in ([], ["alembic_version"]), orphans

    def test_the_migration_files_are_numbered_uniquely(self) -> None:
        prefixes = [path.name.split("_", 1)[0] for path in sorted(VERSIONS.glob("[0-9]*.py"))]
        assert len(prefixes) == len(set(prefixes)), prefixes
        assert re.fullmatch(r"\d{4}", prefixes[-1]) is not None
