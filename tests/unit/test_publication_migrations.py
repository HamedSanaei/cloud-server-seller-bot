"""Behavioural cover for migrations 0039/0040 (STOREFRONT-V2 publication).

- 0039 adds ``technical_metadata`` / ``operator_disabled`` / ``auto_priced``
  to ``sellable_offers`` (missing columns only), then preserves operator
  intent from stored state: disabled rows become operator-blocked, priced
  rows become manual. A healthy database is a no-op.
- 0040 creates ``catalog_sync_state`` once, and is a no-op afterwards.
- Both refuse automatic downgrades.

The real ``upgrade()`` runs against a scripted inspector and a recording
``op`` (same pattern as the 0038 cover), so every branch is exercised
without a database.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0039 = REPO_ROOT / "alembic" / "versions" / "0039_offer_tech_metadata_and_publication.py"
MIGRATION_0040 = REPO_ROOT / "alembic" / "versions" / "0040_catalog_sync_state.py"


def _load(path: Path, name: str) -> ModuleType:
    stub = types.ModuleType("alembic")
    stub.op = types.SimpleNamespace()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = stub
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return module


class _RowCount:
    def __init__(self, count: int) -> None:
        self.rowcount = count


class _FakeBind:
    def __init__(self, rowcount: int = 0) -> None:
        self.statements: list[str] = []
        self._rowcount = rowcount

    def execute(self, statement: Any) -> _RowCount:
        self.statements.append(str(statement))
        return _RowCount(self._rowcount)


class _FakeInspector:
    def __init__(
        self, columns: dict[str, list[str]] | None = None, tables: list[str] | None = None
    ) -> None:
        self._columns = columns or {}
        self._tables = tables or []

    def get_columns(self, table: str) -> list[dict[str, str]]:
        return [{"name": name} for name in self._columns.get(table, [])]

    def get_table_names(self) -> list[str]:
        return list(self._tables)


class _FakeOp:
    def __init__(self, inspector: _FakeInspector, bind: _FakeBind | None = None) -> None:
        self._inspector = inspector
        self._bind = bind or _FakeBind()
        self.added_columns: list[tuple[str, Any]] = []
        self.created_tables: list[str] = []

    def get_bind(self) -> _FakeBind:
        return self._bind

    def add_column(self, table: str, column: Any) -> None:
        self.added_columns.append((table, column))

    def create_table(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.created_tables.append(name)


def _patch(module: ModuleType, op: _FakeOp, monkeypatch: pytest.MonkeyPatch) -> None:
    module.op = op  # type: ignore[attr-defined]
    import sqlalchemy as sa

    monkeypatch.setattr(sa, "inspect", lambda _bind: op._inspector)


BASE_COLUMNS = [
    "id",
    "provider_key",
    "product_id",
    "location_id",
    "name",
    "enabled",
    "selling_price_minor",
]


class TestMigration0039:
    def test_adds_missing_columns_and_backfills_intent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load(MIGRATION_0039, "migration_0039")
        assert module.down_revision == "0038"
        op = _FakeOp(_FakeInspector(columns={"sellable_offers": list(BASE_COLUMNS)}))
        _patch(module, op, monkeypatch)
        module.upgrade()
        assert len(op.added_columns) == 3
        assert all(table == "sellable_offers" for table, _column in op.added_columns)
        # Both intent-preserving updates ran.
        assert len(op._bind.statements) == 2
        assert any("operator_disabled" in statement for statement in op._bind.statements)
        assert any("auto_priced" in statement for statement in op._bind.statements)

    def test_healthy_database_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load(MIGRATION_0039, "migration_0039b")
        op = _FakeOp(
            _FakeInspector(
                columns={
                    "sellable_offers": [
                        *BASE_COLUMNS,
                        "technical_metadata",
                        "operator_disabled",
                        "auto_priced",
                    ]
                }
            )
        )
        _patch(module, op, monkeypatch)
        module.upgrade()
        assert op.added_columns == []
        assert op._bind.statements == []

    def test_downgrade_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load(MIGRATION_0039, "migration_0039c")
        op = _FakeOp(_FakeInspector())
        _patch(module, op, monkeypatch)
        with pytest.raises(RuntimeError):
            module.downgrade()


class TestMigration0040:
    def test_creates_state_table_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load(MIGRATION_0040, "migration_0040")
        assert module.down_revision == "0039"
        op = _FakeOp(_FakeInspector(tables=["sellable_offers"]))
        _patch(module, op, monkeypatch)
        module.upgrade()
        assert op.created_tables == ["catalog_sync_state"]

    def test_existing_table_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load(MIGRATION_0040, "migration_0040b")
        op = _FakeOp(_FakeInspector(tables=["sellable_offers", "catalog_sync_state"]))
        _patch(module, op, monkeypatch)
        module.upgrade()
        assert op.created_tables == []

    def test_downgrade_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load(MIGRATION_0040, "migration_0040c")
        op = _FakeOp(_FakeInspector())
        _patch(module, op, monkeypatch)
        with pytest.raises(RuntimeError):
            module.downgrade()
