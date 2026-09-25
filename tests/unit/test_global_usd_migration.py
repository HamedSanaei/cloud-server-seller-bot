"""Behavioural cover for migration ``0043`` (GLOBAL USD pricing columns).

The incident these tests pin: on a FRESH PostgreSQL database the migration added
its columns and then died inside ``_repair_column_shape`` with

    TypeError: isinstance() arg 2 must be a type, a tuple of types, or a union

The repair loop declared the expected column type as a SQLAlchemy
``TypeEngine`` but the callers handed it a mixture of type CLASSES (``JSONB``)
and type INSTANCES (``sa.Text()``, ``sa.String(length=3)``).  ``isinstance``
accepts only a type, so the first instance in the tuple aborted the migration.
No unit test executed ``upgrade()``, so only the real fresh-database smoke test
could see it.

The real ``upgrade()`` therefore runs here against a scripted inspector whose
reflected types are POSTGRESQL DIALECT types (``postgresql.TEXT()``,
``postgresql.VARCHAR(3)``, ``postgresql.JSONB()``) and whose defaults are
rendered the way PostgreSQL renders them (``'{}'::jsonb``) - the same inputs
``sqlalchemy.inspect`` produces in production - so both the crash and its fix
are reproducible without a database.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "alembic" / "versions" / "0043_global_usd_pricing.py"

#: PostgreSQL's canonical rendering of the JSONB empty-object default.
PG_EMPTY_JSONB_DEFAULT = "'{}'::jsonb"

REPAIR_ENTRIES = 13


def _load() -> ModuleType:
    """Import the migration by path, with an ``alembic.op`` stand-in.

    The repository ships its migration directory as the ``alembic`` package, so
    the local directory shadows the installed distribution here. The stub only
    has to survive module execution; each test then replaces ``module.op``.
    """
    stub = types.ModuleType("alembic")
    stub.op = types.SimpleNamespace()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = stub
    try:
        spec = importlib.util.spec_from_file_location("migration_0043", MIGRATION)
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
    def __init__(self, count: int = 0) -> None:
        self.rowcount = count


class _FakeBind:
    """Records the DML the migration runs instead of executing it."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> _RowCount:
        self.statements.append(str(statement))
        return _RowCount()


class _FakeInspector:
    """Reflected columns, shaped like ``Inspector.get_columns``."""

    def __init__(self, columns: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._columns = columns or {}

    def get_columns(self, table: str) -> list[dict[str, Any]]:
        return list(self._columns.get(table, []))


class _FakeOp:
    """Records DDL instead of executing it."""

    def __init__(self, inspector: _FakeInspector, bind: _FakeBind | None = None) -> None:
        self._inspector = inspector
        self._bind = bind or _FakeBind()
        self.added_columns: list[tuple[str, str]] = []
        self.altered: list[tuple[str, str, dict[str, Any]]] = []

    def get_bind(self) -> _FakeBind:
        return self._bind

    def add_column(self, table: str, column: Any) -> None:
        self.added_columns.append((table, str(column.name)))

    def alter_column(self, table: str, name: str, **kwargs: Any) -> None:
        self.altered.append((table, name, kwargs))


def _patch(module: ModuleType, op: _FakeOp, monkeypatch: pytest.MonkeyPatch) -> None:
    module.op = op  # type: ignore[attr-defined]
    monkeypatch.setattr(sa, "inspect", lambda _bind: op._inspector)


def _column(
    name: str,
    type_: sa.types.TypeEngine[Any],
    *,
    nullable: bool = True,
    default: object | None = None,
) -> dict[str, Any]:
    return {"name": name, "type": type_, "nullable": nullable, "default": default}


def _healthy_columns() -> dict[str, list[dict[str, Any]]]:
    """The physical schema migration 0043 leaves behind on PostgreSQL 16."""
    return {
        "sellable_offers": [
            _column("id", sa.Integer(), nullable=False),
            _column("provider_cost_currency", pg.VARCHAR(3), nullable=False),
            _column(
                "pricing_metadata",
                pg.JSONB(),
                nullable=False,
                default=PG_EMPTY_JSONB_DEFAULT,
            ),
        ],
        "server_price_snapshots": [
            _column("id", sa.Integer(), nullable=False),
            _column("provider_rate_exact", pg.TEXT()),
            _column(
                "pricing_metadata",
                pg.JSONB(),
                nullable=False,
                default=PG_EMPTY_JSONB_DEFAULT,
            ),
            _column("selling_currency", pg.VARCHAR(3)),
            _column("offer_fingerprint", pg.JSONB()),
        ],
        "servers": [
            _column("id", sa.Integer(), nullable=False),
            _column("currency", pg.VARCHAR(3), nullable=False),
            _column("image_id", pg.VARCHAR()),
            _column("offer_fingerprint", pg.JSONB()),
        ],
        "accrual_periods": [
            _column("id", sa.Integer(), nullable=False),
            _column("cost_currency", pg.VARCHAR(3)),
            _column("selling_currency", pg.VARCHAR(3)),
            _column("cost_amount", pg.TEXT()),
            _column("rule_key", pg.VARCHAR()),
        ],
        "provider_orders": [
            _column("id", sa.Integer(), nullable=False),
            _column("provider_monthly_rate_exact", pg.TEXT()),
            _column(
                "pricing_metadata",
                pg.JSONB(),
                nullable=False,
                default=PG_EMPTY_JSONB_DEFAULT,
            ),
        ],
        "wallets": [
            _column("id", sa.Integer(), nullable=False),
            _column("currency", pg.VARCHAR(3), nullable=False, default="'USD'::character varying"),
        ],
    }


def _run(
    monkeypatch: pytest.MonkeyPatch, columns: dict[str, list[dict[str, Any]]]
) -> tuple[ModuleType, _FakeOp]:
    module = _load()
    op = _FakeOp(_FakeInspector(columns))
    _patch(module, op, monkeypatch)
    module.upgrade()
    return module, op


class TestFreshPostgresMigrationSmoke:
    """The CI ``Fresh-PostgreSQL migration smoke`` job, without the database."""

    def test_upgrade_completes_on_a_database_already_at_head(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The crash site: every column exists and is reflected as a PG type."""
        module, op = _run(monkeypatch, _healthy_columns())

        assert str(module.revision) == "0043"
        assert module.down_revision == "0042"
        assert op.added_columns == []
        # ...and no REVISION of the shape was needed for the columns whose
        # PostgreSQL type already matches the intended one.
        assert [entry for entry in op.altered if "type_" in entry[2]] == []

    def test_upgrade_adds_the_columns_of_a_pre_0043_database(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        columns = {
            "sellable_offers": [_column("id", sa.Integer(), nullable=False)],
            "server_price_snapshots": [_column("id", sa.Integer(), nullable=False)],
            "servers": [_column("id", sa.Integer(), nullable=False)],
            "accrual_periods": [_column("id", sa.Integer(), nullable=False)],
            "provider_orders": [_column("id", sa.Integer(), nullable=False)],
            "wallets": [
                _column("id", sa.Integer(), nullable=False),
                _column("currency", pg.VARCHAR(3), nullable=False),
            ],
        }
        _module, op = _run(monkeypatch, columns)

        assert sorted(name for _table, name in op.added_columns) == sorted(
            [
                "pricing_metadata",
                "selling_currency",
                "provider_rate_exact",
                "pricing_metadata",
                "offer_fingerprint",
                "image_id",
                "offer_fingerprint",
                "cost_currency",
                "selling_currency",
                "cost_amount",
                "rule_key",
                "provider_monthly_rate_exact",
                "pricing_metadata",
            ]
        )
        # Nothing is filled or quarantined on a database that has no rows yet.
        assert op._bind.statements == []

    def test_the_repair_loop_never_hands_a_type_class_to_isinstance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Structural guard for the root cause: instances in, never classes."""
        module = _load()
        seen: list[object] = []
        original = module._repair_column_shape

        def _spy(
            table: str,
            name: str,
            *,
            type_: sa.types.TypeEngine[Any],
            nullable: bool,
            server_default: object | None,
        ) -> None:
            seen.append(type_)
            original(
                table,
                name,
                type_=type_,
                nullable=nullable,
                server_default=server_default,
            )

        op = _FakeOp(_FakeInspector(_healthy_columns()))
        _patch(module, op, monkeypatch)
        monkeypatch.setattr(module, "_repair_column_shape", _spy)
        module.upgrade()

        assert len(seen) == REPAIR_ENTRIES
        for expected in seen:
            assert isinstance(expected, sa.types.TypeEngine), expected
            # A class here is exactly what made ``isinstance`` raise.
            assert not isinstance(expected, type), expected


class TestColumnShapeRepair:
    """Drift repair still works, and still never rewrites a money value."""

    def test_a_wrong_type_is_repaired_with_a_textual_cast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        columns = _healthy_columns()
        columns["server_price_snapshots"][1] = _column("provider_rate_exact", sa.Integer())
        columns["accrual_periods"][1] = _column("cost_currency", pg.VARCHAR(255))
        columns["accrual_periods"][3] = _column("rule_key", sa.Integer())
        _module, op = _run(monkeypatch, columns)

        altered = {(table, name): kwargs for table, name, kwargs in op.altered}
        rate_exact = altered[("server_price_snapshots", "provider_rate_exact")]
        assert isinstance(rate_exact["type_"], sa.Text)
        assert isinstance(rate_exact["existing_type"], sa.Integer)
        # A textual widening only: the stored digits are preserved verbatim.
        assert rate_exact["postgresql_using"] == "provider_rate_exact::text"

        cost_currency = altered[("accrual_periods", "cost_currency")]
        assert isinstance(cost_currency["type_"], sa.String)
        assert cost_currency["type_"].length == 3
        assert cost_currency["existing_type"].length == 255

        rule_key = altered[("accrual_periods", "rule_key")]
        assert isinstance(rule_key["type_"], sa.String)
        assert rule_key["type_"].length is None

    def test_a_length_mismatch_is_drift_but_an_unbounded_target_is_not(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        columns = _healthy_columns()
        # VARCHAR(255) where a 3-character currency is intended must be fixed.
        columns["accrual_periods"][2] = _column("selling_currency", pg.VARCHAR(255))
        # ``sa.String()`` deliberately pins no width: any reflected length is
        # compatible and must NOT trigger a needless ALTER ... TYPE.
        columns["servers"][2] = _column("image_id", pg.VARCHAR(255))
        _module, op = _run(monkeypatch, columns)

        altered = {(table, name) for table, name, _kwargs in op.altered}
        assert ("accrual_periods", "selling_currency") in altered
        assert ("servers", "image_id") not in altered

    def test_nullability_drift_is_repaired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        columns = _healthy_columns()
        columns["sellable_offers"][2] = _column("pricing_metadata", pg.JSONB(), nullable=True)
        _module, op = _run(monkeypatch, columns)

        tightened = [
            kwargs
            for table, name, kwargs in op.altered
            if (table, name) == ("sellable_offers", "pricing_metadata")
            and kwargs.get("nullable") is False
        ]
        assert len(tightened) == 1
        # The JSONB type already matches, so only nullability moves: no type
        # change and no ``USING`` cast is emitted for this column.
        assert isinstance(tightened[0]["existing_type"], pg.JSONB)
        assert "type_" not in tightened[0]
        assert "postgresql_using" not in tightened[0]

    def test_no_statement_writes_a_money_column(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The only DML target is the non-financial JSON audit projection."""
        _module, op = _run(monkeypatch, _healthy_columns())

        # 3 metadata fills + 2 legacy-exact quarantine markers.
        assert len(op._bind.statements) == 5
        for statement in op._bind.statements:
            set_clause = statement.split("SET ", 1)[1].split(" WHERE", 1)[0]
            assert set_clause.startswith("pricing_metadata="), statement
        joined = " ".join(op._bind.statements)
        for money_column in ("cost_amount", "provider_rate_exact", "provider_monthly_rate_exact"):
            assert f"SET {money_column}" not in joined


class TestServerDefaultComparison:
    """Defaults are compared, never guessed - and never re-issued needlessly."""

    def test_a_postgres_rendered_default_is_not_drift(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _module, op = _run(monkeypatch, _healthy_columns())

        touched = {(table, name) for table, name, _kwargs in op.altered}
        # The JSONB ``{}`` default reads back as ``'{}'::jsonb``: the same
        # default, so it must not re-issue ``SET DEFAULT`` on every pass.
        assert ("sellable_offers", "pricing_metadata") not in touched
        assert ("server_price_snapshots", "pricing_metadata") not in touched
        assert ("provider_orders", "pricing_metadata") not in touched

    def test_the_jsonb_default_is_only_written_when_it_is_actually_different(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        columns = _healthy_columns()
        columns["server_price_snapshots"][2] = _column(
            "pricing_metadata", pg.JSONB(), nullable=False, default="'[]'::jsonb"
        )
        _module, op = _run(monkeypatch, columns)

        written = {
            name: kwargs
            for table, name, kwargs in op.altered
            if (table, name) == ("server_price_snapshots", "pricing_metadata")
        }
        assert written["pricing_metadata"]["server_default"] == "{}"
        # The other two JSONB columns already carry the intended default.
        assert [name for table, name, _kwargs in op.altered].count("pricing_metadata") == 1

    def test_only_the_creation_policy_defaults_are_rewritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database at head needs exactly the three default-policy alters."""
        _module, op = _run(monkeypatch, _healthy_columns())

        assert [(table, name) for table, name, _kwargs in op.altered] == [
            ("sellable_offers", "provider_cost_currency"),
            ("servers", "currency"),
            ("wallets", "currency"),
        ]
        by_column = {(table, name): kwargs for table, name, kwargs in op.altered}
        for table, column in (
            ("sellable_offers", "provider_cost_currency"),
            ("servers", "currency"),
        ):
            # The historical EUR default must not survive as a native-cost label.
            assert by_column[(table, column)]["server_default"] is None
        assert by_column[("wallets", "currency")]["server_default"] == "USD"


class TestTypeComparator:
    @pytest.mark.parametrize(
        ("current", "expected", "matches"),
        [
            # PostgreSQL reflects dialect subclasses of the generic type.
            (pg.TEXT(), sa.Text(), True),
            (pg.VARCHAR(3), sa.String(length=3), True),
            (pg.VARCHAR(255), sa.String(), True),
            (pg.JSONB(), pg.JSONB(), True),
            (sa.TEXT(), sa.Text(), True),
            (sa.Text(), sa.Text(), True),
            # A different shape is drift, not a match.
            (pg.VARCHAR(255), sa.String(length=3), False),
            (pg.VARCHAR(), sa.String(length=3), False),
            (sa.Integer(), sa.Text(), False),
            (sa.Integer(), sa.String(length=3), False),
            (sa.JSON(), pg.JSONB(), False),
        ],
    )
    def test_shape_comparison(
        self, current: object, expected: sa.types.TypeEngine[Any], matches: bool
    ) -> None:
        assert _load()._type_matches(current, expected) is matches

    def test_an_unknown_reflected_type_is_never_a_match(self) -> None:
        assert _load()._type_matches(None, sa.Text()) is False

    @pytest.mark.parametrize(
        ("raw", "normalized"),
        [
            ("'{}'::jsonb", "{}"),
            ("'{}'", "{}"),
            ("{}", "{}"),
            ("'USD'::character varying", "USD"),
            ("USD", "USD"),
            (None, ""),
            ("", ""),
            # Anything that is not a quoted literal is left alone.
            ("nextval('seq'::regclass)", "nextval('seq'::regclass)"),
        ],
    )
    def test_default_normalization_is_comparison_only(self, raw: object, normalized: str) -> None:
        assert _load()._normalize_default(raw) == normalized

    def test_distinct_defaults_are_not_normalized_together(self) -> None:
        module = _load()
        assert module._normalize_default("'EUR'::character varying") != module._normalize_default(
            "USD"
        )
        assert module._normalize_default("'{}'::jsonb") != module._normalize_default("'[]'::jsonb")


class TestMigration0043Invariants:
    def test_downgrade_refuses_to_destroy_pricing_audit(self) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            _load().downgrade()
        assert "no automatic downgrade" in str(excinfo.value)

    def test_the_migration_never_runs_raw_sql(self) -> None:
        source = MIGRATION.read_text(encoding="utf-8")
        assert "op.execute" not in source
        assert "text(" not in source
