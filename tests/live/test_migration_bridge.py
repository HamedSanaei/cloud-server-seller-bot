"""Opt-in, rollback-only PostgreSQL proof for the ambiguous historical 0050.

Only CLOUD_PLATFORM_TEST_POSTGRES_URL is accepted, never application settings.
Every probe creates its own schema inside a transaction and rolls it back;
it neither migrates nor stamps the existing database's application schema.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="requires an isolated scratch PostgreSQL database"
)


def _run_probe(start: str) -> None:
    """Run from a subprocess cwd that cannot shadow installed Alembic."""
    import importlib.util

    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    from cloud_platform.db.schema_parity import evaluate, observe_schema

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["0060"]
    engine = sa.create_engine(
        os.environ["CLOUD_PLATFORM_TEST_POSTGRES_URL"].replace(
            "postgresql+asyncpg://", "postgresql://"
        ),
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                schema = "bridge_test_" + uuid4().hex
                connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
                connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}", public'))

                def upgrade(target):
                    context = MigrationContext.configure(
                        connection,
                        opts={
                            "version_table_schema": schema,
                            "fn": lambda revisions, _context: scripts._upgrade_revs(
                                target, revisions
                            ),
                        },
                    )
                    with Operations.context(context):
                        context.run_migrations()

                def table(name):
                    return sa.Table(name, sa.MetaData(), autoload_with=connection)

                def insert(table_name, **values):
                    target = table(table_name)
                    connection.execute(target.insert().values(**values))

                if start == "fresh":
                    upgrade("head")
                    assert evaluate(observe_schema(sa.inspect(connection))).ok
                    _assert_physical_shape(connection)
                    return
                if start == "historical0049":
                    upgrade("0049")
                    upgrade("head")
                    assert evaluate(observe_schema(sa.inspect(connection))).ok
                    _assert_physical_shape(connection)
                    return

                upgrade("0049" if start == "main0050" else "0050")
                user_id, wallet_id, provider_id, account_id = (uuid4() for _ in range(4))
                server_id, requested_id, deleted_id, payment_id = (uuid4() for _ in range(4))
                created = datetime(2026, 1, 1, 1, 2, 3)
                insert("users", id=user_id, username="bridge", email="bridge@example.test")
                insert("wallets", id=wallet_id, user_id=user_id, balance=12345, currency="IRT")
                insert("providers", id=provider_id, name="fixture-provider")
                insert(
                    "provider_accounts",
                    id=account_id,
                    user_id=user_id,
                    provider_id=provider_id,
                    status="active",
                )
                for identity, state, external_id in (
                    (server_id, "running", "historical-server"),
                    (requested_id, "requested", None),
                    (deleted_id, "deleted", "deleted-server"),
                ):
                    insert(
                        "servers",
                        id=identity,
                        user_id=user_id,
                        provider_id=provider_id,
                        provider_account_id=account_id,
                        price_per_quantum=7,
                        currency="EUR",
                        billing_model="hourly",
                        created_at=created,
                        state=state,
                        provider_server_id=external_id,
                        last_accrued_at=created.replace(tzinfo=UTC) + timedelta(hours=4),
                    )

                if start == "main0050":
                    path = ROOT / "tests/fixtures/migrations/main_0050.py"
                    spec = importlib.util.spec_from_file_location("frozen_main_0050", path)
                    assert spec is not None and spec.loader is not None
                    artifact = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(artifact)
                    artifact.op = Operations(MigrationContext.configure(connection))
                    artifact.upgrade()
                    # Fixture bookkeeping after physically applying the exact
                    # historical artifact; this is NOT a deployment repair.
                    MigrationContext.configure(
                        connection, opts={"version_table_schema": schema}
                    ).stamp(scripts, "0050")
                    insert(
                        "payment_gateway_settings",
                        key="atlaspay",
                        enabled=False,
                        updated_by=user_id,
                    )
                    users = table("users")
                    connection.execute(
                        users.update()
                        .where(users.c.id == user_id)
                        .values(
                            phone_number="+989120000001",
                            national_id="1234567890",
                            phone_verified_at=created.replace(tzinfo=UTC),
                        )
                    )
                    servers = table("servers")
                    connection.execute(
                        servers.update()
                        .where(servers.c.id == server_id)
                        .values(
                            billing_started_at=created.replace(tzinfo=UTC) + timedelta(minutes=5)
                        )
                    )

                payment_values = {
                    "id": payment_id,
                    "user_id": user_id,
                    "gateway_key": "atlaspay",
                    "gateway_payment_id": "historical-order",
                    "amount_minor": 1234,
                    "currency": "IRT",
                    "idempotency_key": "immutable-merchant-ref",
                    "credit_amount_minor": 1200,
                    "credit_currency": "IRT",
                    "fx_source": "historical",
                    "fx_rate": "1.00000000",
                }
                invoice = {"payment_url": "https://example.test/invoice", "tracking_code": "track"}
                if start == "main0050":
                    payment_values["payment_details"] = {**invoice, "audit_extension": "keep"}
                else:
                    payment_values.update(
                        redirect_url=invoice["payment_url"], tracking_code="track"
                    )
                insert("payment_sessions", **payment_values)

                if start != "staging0050":
                    upgrade("0053")
                    servers = table("servers")
                    connection.execute(
                        servers.update()
                        .where(servers.c.id == server_id)
                        .values(
                            prepaid_paid_until=created + timedelta(hours=1),
                            prepaid_zero_since=created + timedelta(minutes=30),
                        )
                    )
                    insert(
                        "prepaid_hourly_periods",
                        id=uuid4(),
                        server_id=server_id,
                        wallet_id=wallet_id,
                        period_start=created,
                        period_end=created + timedelta(hours=1),
                        usd_minor=7,
                        irt_minor=333,
                        fx_snapshot={"rate": "47.571428571428", "evidence": "keep"},
                        idempotency_key="frozen-hour",
                        status="paid",
                    )
                    insert(
                        "wallet_currency_migrations",
                        id=uuid4(),
                        user_id=user_id,
                        wallet_id=wallet_id,
                        source_balance_minor=123,
                        snapshot={"rate": "101.123456789", "audit": "keep"},
                        operator_id=user_id,
                        reason="historical fixture",
                        created_at=created.replace(tzinfo=UTC),
                    )
                    insert(
                        "ledger",
                        id=uuid4(),
                        wallet_id=wallet_id,
                        entry_type="deposit",
                        amount=12345,
                        currency="IRT",
                        idempotency_key="frozen-ledger",
                    )

                immutable_tables = ["wallets", "ledger"]
                if start != "staging0050":
                    immutable_tables += ["prepaid_hourly_periods", "wallet_currency_migrations"]
                # Compare every historical column, not new nullable receipt
                # columns added by subsequent additive migrations.
                historical_columns = {name: list(table(name).c) for name in immutable_tables}
                before = {
                    name: connection.execute(sa.select(*columns)).all()
                    for name, columns in historical_columns.items()
                }
                server_columns = list(table("servers").c)
                server_facts = connection.execute(
                    sa.select(*server_columns).order_by(server_columns[0])
                ).all()

                if start == "conflicting_projection":
                    connection.execute(
                        sa.text("ALTER TABLE payment_sessions ADD COLUMN payment_details JSONB")
                    )
                    payments = table("payment_sessions")
                    connection.execute(
                        payments.update().values(
                            payment_details={
                                "payment_url": invoice["payment_url"],
                                "tracking_code": "different",
                            }
                        )
                    )
                    with pytest.raises((RuntimeError, ValueError)), connection.begin_nested():
                        upgrade("head")
                    assert (
                        connection.execute(
                            sa.text(f'SELECT version_num FROM "{schema}".alembic_version')
                        ).scalar_one()
                        == "0053"
                    )
                    assert (
                        connection.execute(sa.select(payments.c.payment_details)).scalar_one()[
                            "tracking_code"
                        ]
                        == "different"
                    )
                    return

                if start.startswith("duplicate_"):
                    gateway = start.removeprefix("duplicate_")
                    if gateway == "atlaspay":
                        connection.execute(sa.text("DROP INDEX uq_atlaspay_intent"))
                    payments = table("payment_sessions")
                    connection.execute(
                        payments.update()
                        .where(payments.c.id == payment_id)
                        .values(gateway_key=gateway)
                    )
                    duplicate = dict(
                        payment_values,
                        id=uuid4(),
                        gateway_key=gateway,
                        gateway_payment_id="different-order",
                    )
                    insert("payment_sessions", **duplicate)
                    with (
                        pytest.raises((RuntimeError, ValueError, sa.exc.IntegrityError)),
                        connection.begin_nested(),
                    ):
                        upgrade("head")
                    assert (
                        connection.execute(
                            sa.text(f'SELECT version_num FROM "{schema}".alembic_version')
                        ).scalar_one()
                        == "0053"
                    )
                    assert "payment_details" not in {
                        c["name"] for c in sa.inspect(connection).get_columns("payment_sessions")
                    }
                    return

                upgrade("head")
                assert evaluate(observe_schema(sa.inspect(connection))).ok
                _assert_physical_shape(connection)
                for name, rows in before.items():
                    assert connection.execute(sa.select(*historical_columns[name])).all() == rows, (
                        name
                    )
                assert (
                    connection.execute(sa.select(*server_columns).order_by(server_columns[0])).all()
                    == server_facts
                )
                payments = table("payment_sessions")
                payment = (
                    connection.execute(sa.select(payments).where(payments.c.id == payment_id))
                    .mappings()
                    .one()
                )
                for key, value in payment_values.items():
                    assert payment[key] == value, key
                assert payment["redirect_url"] == invoice["payment_url"]
                assert payment["tracking_code"] == "track"
                for key, value in invoice.items():
                    assert payment["payment_details"][key] == value
                servers = table("servers")
                anchors = dict(
                    connection.execute(sa.select(servers.c.id, servers.c.billing_started_at)).all()
                )
                assert anchors[server_id] == created.replace(tzinfo=UTC) + (
                    timedelta(minutes=5) if start == "main0050" else timedelta(0)
                )
                assert anchors[deleted_id] == created.replace(tzinfo=UTC)
                assert anchors[requested_id] is None
                if start == "main0050":
                    assert (
                        connection.execute(
                            sa.select(table("payment_gateway_settings").c.enabled)
                        ).scalar_one()
                        is False
                    )
                    user = connection.execute(sa.select(table("users"))).mappings().one()
                    assert user["phone_number"] == "+989120000001"
                    assert user["phone_verified_at"] == created.replace(tzinfo=UTC)
                    assert user["national_id"] == "1234567890"
                # Directly reapply only the additive bridge to prove its guards
                # are non-destructive even though Alembic itself runs it once.
                bridge = next((ROOT / "alembic/versions").glob("0054_*.py"))
                spec = importlib.util.spec_from_file_location("bridge_again", bridge)
                assert spec is not None and spec.loader is not None
                artifact = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(artifact)
                artifact.op = Operations(MigrationContext.configure(connection))
                artifact.upgrade()
                for name, rows in before.items():
                    assert connection.execute(sa.select(*historical_columns[name])).all() == rows, (
                        name
                    )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def _assert_physical_shape(connection) -> None:
    from cloud_platform.db.schema_parity import _predicate

    inspector = sa.inspect(connection)
    users = {c["name"]: c for c in inspector.get_columns("users")}
    assert isinstance(users["phone_number"]["type"], sa.String)
    assert users["phone_number"]["type"].length == 16
    assert users["national_id"]["type"].length == 10
    assert users["phone_verified_at"]["type"].timezone is True
    servers = {c["name"]: c for c in inspector.get_columns("servers")}
    assert servers["billing_started_at"]["type"].timezone is True
    assert servers["prepaid_paid_until"]["type"].timezone is False
    assert servers["prepaid_zero_since"]["type"].timezone is False
    payments = {c["name"]: c for c in inspector.get_columns("payment_sessions")}
    from sqlalchemy.dialects.postgresql import JSONB

    assert isinstance(payments["payment_details"]["type"], JSONB)
    assert isinstance(payments["redirect_url"]["type"], sa.Text)
    assert isinstance(payments["tracking_code"]["type"], sa.Text)
    indexes = [
        index
        for index in inspector.get_indexes("payment_sessions")
        if index["unique"] and set(index["column_names"]) == {"gateway_key", "idempotency_key"}
    ]
    partial = [index for index in indexes if index["name"] == "uq_atlaspay_merchant_ref"]
    assert len(partial) == 1
    assert (
        _predicate(partial[0]["dialect_options"]["postgresql_where"]) == "gateway_key = 'atlaspay'"
    )
    assert any(
        index["name"] == "uq_payment_sessions_gateway_idempotency"
        and not index.get("dialect_options", {}).get("postgresql_where")
        for index in indexes
    )
    for name, constraint in (
        ("prepaid_hourly_periods", "ck_prepaid_hourly_positive"),
        ("wallet_currency_migrations", "ck_wallet_migration_nonnegative"),
    ):
        assert constraint in {item["name"] for item in inspector.get_check_constraints(name)}
    gateway_fks = inspector.get_foreign_keys("payment_gateway_settings")
    assert any(
        fk["referred_table"] == "users" and fk["options"].get("ondelete") == "SET NULL"
        for fk in gateway_fks
    )
    periods_unique = {
        frozenset(item["column_names"])
        for item in inspector.get_unique_constraints("prepaid_hourly_periods")
    }
    assert frozenset({"server_id", "period_start"}) in periods_unique
    assert frozenset({"idempotency_key"}) in periods_unique
    period_fks = inspector.get_foreign_keys("prepaid_hourly_periods")
    historical_fks = [
        item for item in period_fks if item["referred_table"] in {"servers", "wallets"}
    ]
    assert {item["referred_table"] for item in historical_fks} == {"servers", "wallets"}
    assert all(item["options"].get("ondelete") == "CASCADE" for item in historical_fks)
    corporate_fks = [item for item in period_fks if item not in historical_fks]
    assert {item["referred_table"] for item in corporate_fks} == {
        "users",
        "operator_funding_grants",
        "operator_expenses",
    }
    assert all(item["options"].get("ondelete") == "RESTRICT" for item in corporate_fks)
    migration_fks = inspector.get_foreign_keys("wallet_currency_migrations")
    assert len(migration_fks) == 5
    assert all(item["options"].get("ondelete") == "RESTRICT" for item in migration_fks)


@pytest.mark.parametrize(
    "start",
    [
        "fresh",
        "historical0049",
        "main0050",
        "staging0050",
        "staging0053",
        "duplicate_atlaspay",
        "duplicate_tetraminator",
        "duplicate_zarinpal",
        "conflicting_projection",
    ],
)
def test_0054_bridges_real_historical_physical_schemas(tmp_path, start):
    command = (
        "import runpy, sys; "
        "namespace = runpy.run_path(sys.argv[1]); namespace['_run_probe'](sys.argv[2])"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, str(Path(__file__).resolve()), start],
        cwd=tmp_path,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
