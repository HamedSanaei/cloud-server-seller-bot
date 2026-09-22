"""Behavioural cover for migration ``0038`` (forward repair of schema drift).

The production incident: a drifted database was missing ``provider_routes`` and
the credential-account columns while code that needs them was already running,
and offer upserts failed on a separate ORM bug. Repairing that forward is a
different kind of migration — it must be IDEMPOTENT (a no-op on a healthy
database), it must never DROP or RECREATE anything, it must never rewrite the
historical offers that migration 0037 pinned, and it must never GUESS which
credential account owns pre-existing rows.

These tests drive the real ``upgrade()`` against a scripted inspector and a
recording ``op``, so every branch is covered without a database.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "alembic" / "versions" / "0038_repair_multi_account_schema_drift.py"
MIGRATION_SOURCE = MIGRATION.read_text(encoding="utf-8")

ROUTES_INDEX = "ix_provider_routes_provider_location"
ROUTES_CONSTRAINT = "uq_provider_routes_account_location"


def _is_select(node: ast.AST) -> bool:
    """Whether a (possibly chained) statement bottoms out in ``sa.select``."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if (
            node.func.attr == "select"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sa"
        ):
            return True
        node = node.func.value
    return False


def _load() -> ModuleType:
    """Import the migration by path, with an ``alembic.op`` stand-in.

    This repository ships its migration directory as the ``alembic`` package, so
    the local directory shadows the installed distribution and a migration's
    ``from alembic import op`` would fail here (in production the console script
    and the container path resolve the real package). The stub only has to
    survive module execution: each test then replaces ``module.op`` with a
    recording fake.
    """
    stub = types.ModuleType("alembic")
    stub.op = types.SimpleNamespace()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = stub
    try:
        spec = importlib.util.spec_from_file_location("repair_0038", MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return module


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value


class _FakeBind:
    """Per-table row counts, chosen by which table the SELECT reads."""

    def __init__(self, **counts: int) -> None:
        self.counts = counts
        self.reads: list[str] = []

    def execute(self, statement: Any) -> _FakeResult:
        sql = str(statement)
        table = "provider_orders" if "provider_orders" in sql else "servers"
        self.reads.append(table)
        return _FakeResult(self.counts.get(table, 0))


class _FakeOp:
    """Records DDL instead of executing it."""

    def __init__(self, bind: _FakeBind | None = None) -> None:
        self._bind = bind or _FakeBind()
        self.calls: list[tuple[str, str]] = []

    def get_bind(self) -> _FakeBind:
        return self._bind

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("create_table", name))

    def create_index(self, name: str, table: str, columns: Any, **kwargs: Any) -> None:
        self.calls.append(("create_index", name))

    def create_unique_constraint(self, name: str, table: str, columns: Any) -> None:
        self.calls.append(("create_unique_constraint", name))

    def add_column(self, table: str, column: Any, **kwargs: Any) -> None:
        self.calls.append(("add_column", f"{table}.{column.name}"))

    def drop_column(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        self.calls.append(("drop_column", "REFUSED"))

    def drop_table(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        self.calls.append(("drop_table", "REFUSED"))


class _FakeInspector:
    def __init__(
        self,
        *,
        tables: set[str],
        columns: dict[str, set[str]] | None = None,
        constraints: dict[str, set[str]] | None = None,
        indexes: dict[str, set[str]] | None = None,
    ) -> None:
        self.tables = tables
        self.columns = columns or {}
        self.constraints = constraints or {}
        self.indexes = indexes or {}

    def get_table_names(self) -> list[str]:
        return sorted(self.tables)

    def get_columns(self, table: str) -> list[dict[str, str]]:
        return [{"name": name} for name in sorted(self.columns.get(table, set()))]

    def get_unique_constraints(self, table: str) -> list[dict[str, str]]:
        return [{"name": name} for name in sorted(self.constraints.get(table, set()))]

    def get_indexes(self, table: str) -> list[dict[str, str]]:
        return [{"name": name} for name in sorted(self.indexes.get(table, set()))]


def _install(
    monkeypatch: pytest.MonkeyPatch, *, inspector: _FakeInspector, bind: _FakeBind
) -> tuple[ModuleType, _FakeOp]:
    module = _load()
    fake_op = _FakeOp(bind)
    monkeypatch.setattr(module, "op", fake_op)
    monkeypatch.setattr(module.sa, "inspect", lambda _bind: inspector)
    return module, fake_op


def _upgrade(
    monkeypatch: pytest.MonkeyPatch, *, inspector: _FakeInspector, bind: _FakeBind
) -> _FakeOp:
    module, fake_op = _install(monkeypatch, inspector=inspector, bind=bind)
    module.upgrade()
    return fake_op


def _upgrade_failing(
    monkeypatch: pytest.MonkeyPatch, *, inspector: _FakeInspector, bind: _FakeBind
) -> tuple[str, _FakeOp]:
    module, fake_op = _install(monkeypatch, inspector=inspector, bind=bind)
    with pytest.raises(RuntimeError) as excinfo:
        module.upgrade()
    return str(excinfo.value), fake_op


def _healthy_inspector() -> _FakeInspector:
    return _FakeInspector(
        tables={"provider_routes", "provider_orders", "servers", "providers"},
        columns={
            "provider_orders": {"id", "provider_key", "credential_account_id"},
            "servers": {"id", "provider_id", "credential_account_id"},
        },
        constraints={"provider_routes": {ROUTES_CONSTRAINT}},
        indexes={"provider_routes": {ROUTES_INDEX}},
    )


def _drifted_inspector(*, with_routes: bool = False) -> _FakeInspector:
    """The incident database: no provider_routes, no credential columns."""
    tables = {"provider_orders", "servers", "providers"}
    if with_routes:
        tables.add("provider_routes")
    return _FakeInspector(
        tables=tables,
        columns={
            "provider_orders": {"id", "provider_key"},
            "servers": {"id", "provider_id"},
        },
        constraints={},
        indexes={},
    )


def _acknowledge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, body: str | None = None
) -> Path:
    config = tmp_path / "configuration.toml"
    config.write_text(
        body
        if body is not None
        else "[database]\nacknowledge_unproven_credential_accounts = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLOUD_PLATFORM_CONFIG_FILE", str(config))
    return config


class TestNoOpOnAHealthyDatabase:
    def test_nothing_is_created_or_altered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        op = _upgrade(
            monkeypatch,
            inspector=_healthy_inspector(),
            bind=_FakeBind(provider_orders=0, servers=0),
        )
        assert op.calls == []

    def test_existing_history_is_irrelevant_when_no_column_must_be_added(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard exists for a MISSING column, not for existing history."""
        op = _upgrade(
            monkeypatch,
            inspector=_healthy_inspector(),
            bind=_FakeBind(provider_orders=12, servers=7),
        )
        assert op.calls == []


class TestTheDriftedDatabase:
    """This production: zero historical Leaseweb orders/servers."""

    def test_creates_the_missing_objects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        op = _upgrade(
            monkeypatch,
            inspector=_drifted_inspector(),
            bind=_FakeBind(provider_orders=0, servers=0),
        )
        assert op.calls == [
            ("create_table", "provider_routes"),
            ("create_index", ROUTES_INDEX),
            ("add_column", "provider_orders.credential_account_id"),
            ("add_column", "servers.credential_account_id"),
        ]

    def test_no_table_is_dropped_or_recreated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        op = _upgrade(
            monkeypatch,
            inspector=_drifted_inspector(with_routes=True),
            bind=_FakeBind(provider_orders=0, servers=0),
        )
        assert not [call for call in op.calls if call[0].startswith("drop")]
        assert not [call for call in op.calls if call[0] == "create_table"]

    def test_a_partially_created_table_gets_its_keys_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inspector = _FakeInspector(
            tables={"provider_routes", "provider_orders", "servers", "providers"},
            columns={
                "provider_orders": {"id", "provider_key", "credential_account_id"},
                "servers": {"id", "provider_id", "credential_account_id"},
            },
            constraints={},
            indexes={},
        )
        op = _upgrade(monkeypatch, inspector=inspector, bind=_FakeBind())
        assert op.calls == [
            ("create_unique_constraint", ROUTES_CONSTRAINT),
            ("create_index", ROUTES_INDEX),
        ]

    def test_repair_never_writes_an_ownership_value(self) -> None:
        """Blunt but decisive: the module only ever READS the database.

        ``op.execute`` is alembic's DML escape hatch, so it is banned outright;
        every statement the migration runs must be a ``sa.select`` (the row
        count behind the fail-closed guard).
        """
        tree = ast.parse(MIGRATION_SOURCE)
        assert "op.execute" not in MIGRATION_SOURCE
        assert ".values(" not in MIGRATION_SOURCE
        assert "insert(" not in MIGRATION_SOURCE
        statements = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
        ]
        assert statements, "the fail-closed guard must actually read the database"
        for call in statements:
            assert _is_select(call.args[0]), ast.dump(call.args[0])
        # ...and it never touches the offers migration 0037 pinned to `default`:
        # neither as a DDL target nor as a DML table.
        touched = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {
                "create_table",
                "create_index",
                "create_unique_constraint",
                "add_column",
                "drop_table",
                "drop_column",
                "table",
            }
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        # (`provider_routes` is referenced through the module constant, and the
        # schema-parity suite pins which revisions may create it.)
        assert touched == {"provider_orders", "servers", "providers"}, touched
        assert "sellable_offers" not in touched, touched


class TestFailClosedOnUnprovableOwnership:
    """Other installations: history that cannot be attributed to a key."""

    def test_refuses_before_any_ddl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLOUD_PLATFORM_CONFIG_FILE", raising=False)
        message, op = _upgrade_failing(
            monkeypatch,
            inspector=_drifted_inspector(),
            bind=_FakeBind(provider_orders=3, servers=2),
        )
        assert "0038 must add credential_account_id" in message
        assert "5 row(s)" in message
        assert "leaseweb" in message
        assert "will NOT guess" in message
        assert "acknowledge_unproven_credential_accounts = true" in message
        # Fail closed BEFORE touching the schema: nothing was changed at all.
        assert op.calls == []

    def test_an_operator_acknowledgement_lets_the_schema_forward(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _acknowledge(monkeypatch, tmp_path)
        op = _upgrade(
            monkeypatch,
            inspector=_drifted_inspector(),
            bind=_FakeBind(provider_orders=3, servers=2),
        )
        # The columns are added; no ownership value is written for those rows.
        assert op.calls == [
            ("create_table", "provider_routes"),
            ("create_index", ROUTES_INDEX),
            ("add_column", "provider_orders.credential_account_id"),
            ("add_column", "servers.credential_account_id"),
        ]

    def test_a_malformed_configuration_file_is_not_an_acknowledgement(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _acknowledge(monkeypatch, tmp_path, body="[database\nbroken = ")
        _message, op = _upgrade_failing(
            monkeypatch,
            inspector=_drifted_inspector(),
            bind=_FakeBind(provider_orders=1, servers=0),
        )
        assert op.calls == []

    def test_the_acknowledgement_key_is_section_scoped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A same-named key in another section must not unlock the repair."""
        _acknowledge(
            monkeypatch,
            tmp_path,
            body="acknowledge_unproven_credential_accounts = true\n",
        )
        _upgrade_failing(
            monkeypatch,
            inspector=_drifted_inspector(),
            bind=_FakeBind(provider_orders=1, servers=0),
        )

    def test_the_acknowledgement_must_be_truthy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _acknowledge(
            monkeypatch,
            tmp_path,
            body="[database]\nacknowledge_unproven_credential_accounts = false\n",
        )
        _upgrade_failing(
            monkeypatch,
            inspector=_drifted_inspector(),
            bind=_FakeBind(provider_orders=1, servers=0),
        )


class TestRepairRefusesToHideBiggerDrift:
    def test_a_missing_table_is_reported_instead_of_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inspector = _FakeInspector(tables={"servers", "providers"}, columns={"servers": {"id"}})
        message, op = _upgrade_failing(monkeypatch, inspector=inspector, bind=_FakeBind())
        assert "provider_orders does not exist" in message
        assert op.calls == []

    def test_downgrade_refuses_to_destroy_state(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            _load().downgrade()
        assert "no automatic downgrade" in str(excinfo.value)

    def test_revision_metadata(self) -> None:
        module = _load()
        assert module.revision == "0038"
        assert module.down_revision == "0037"
        assert module.ROUTES_TABLE == "provider_routes"
