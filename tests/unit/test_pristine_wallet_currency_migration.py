"""Regression coverage for migration 0044's zero-value wallet repair."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "alembic" / "versions" / "0044_canonicalize_pristine_wallet_currency.py"


def _load() -> ModuleType:
    stub = types.ModuleType("alembic")
    stub.op = types.SimpleNamespace()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = stub
    try:
        spec = importlib.util.spec_from_file_location("migration_0044", MIGRATION)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return module


def _schema() -> tuple[sa.MetaData, dict[str, sa.Table]]:
    metadata = sa.MetaData()
    tables = {
        "wallets": sa.Table(
            "wallets",
            metadata,
            sa.Column("id", sa.String, primary_key=True),
            sa.Column("user_id", sa.String, nullable=False),
            sa.Column("balance", sa.BigInteger, nullable=False),
            sa.Column("currency", sa.String(3), nullable=False),
        ),
        "ledger": sa.Table("ledger", metadata, sa.Column("wallet_id", sa.String, nullable=False)),
        "holds": sa.Table("holds", metadata, sa.Column("wallet_id", sa.String, nullable=False)),
        "servers": sa.Table("servers", metadata, sa.Column("user_id", sa.String, nullable=False)),
        "payment_sessions": sa.Table(
            "payment_sessions", metadata, sa.Column("user_id", sa.String, nullable=False)
        ),
    }
    return metadata, tables


def test_upgrade_only_canonicalizes_truly_pristine_zero_balance_wallets() -> None:
    module = _load()
    metadata, tables = _schema()
    engine = sa.create_engine("sqlite://")
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            tables["wallets"].insert(),
            [
                {"id": "pristine", "user_id": "u1", "balance": 0, "currency": "EUR"},
                {"id": "funded", "user_id": "u2", "balance": 1, "currency": "EUR"},
                {"id": "ledger", "user_id": "u3", "balance": 0, "currency": "EUR"},
                {"id": "hold", "user_id": "u4", "balance": 0, "currency": "EUR"},
                {"id": "server", "user_id": "u5", "balance": 0, "currency": "EUR"},
                {"id": "payment", "user_id": "u6", "balance": 0, "currency": "EUR"},
                {"id": "already-usd", "user_id": "u7", "balance": 0, "currency": "USD"},
            ],
        )
        connection.execute(tables["ledger"].insert().values(wallet_id="ledger"))
        connection.execute(tables["holds"].insert().values(wallet_id="hold"))
        connection.execute(tables["servers"].insert().values(user_id="u5"))
        connection.execute(tables["payment_sessions"].insert().values(user_id="u6"))

        module.op = types.SimpleNamespace(get_bind=lambda: connection)
        module.upgrade()

        rows = {
            row.id: row.currency
            for row in connection.execute(
                sa.select(tables["wallets"].c.id, tables["wallets"].c.currency)
            )
        }

    assert rows["pristine"] == "USD"
    assert rows["already-usd"] == "USD"
    assert rows["funded"] == "EUR"
    assert rows["ledger"] == "EUR"
    assert rows["hold"] == "EUR"
    assert rows["server"] == "EUR"
    assert rows["payment"] == "EUR"


def test_downgrade_refuses_to_guess_previous_currency() -> None:
    with pytest.raises(RuntimeError, match="no automatic downgrade"):
        _load().downgrade()
