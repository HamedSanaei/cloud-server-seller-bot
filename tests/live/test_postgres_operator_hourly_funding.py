"""Real corporate receipts and stale-customer debit fences in isolated PostgreSQL.

Uses only the explicit scratch loopback test URL accepted by the shared
charge-fence fixture. Actual 0056/0058 upgrades establish the financial tables.
"""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import MetaData, select, text
from sqlalchemy.schema import DefaultClause

from cloud_platform.db.base import AuditEvent, TelegramSuperadminAssignment
from cloud_platform.db.base import LedgerEntry as LedgerRow
from cloud_platform.db.base import PrepaidHourlyPeriod as PeriodRow
from cloud_platform.db.base import Server as ServerRow
from cloud_platform.db.base import User as UserRow
from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.modules.billing.repository import SqlAlchemyPrepaidHourlyPeriodRepository
from cloud_platform.modules.billing.service import PrepaidHourlyBillingService
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.funding import FundingService, FundingSource
from cloud_platform.modules.funding.repository import SqlAlchemyOperatorFundingRepository
from cloud_platform.modules.users.superadmin_repository import SqlAlchemySuperadminRepository
from cloud_platform.modules.wallet.domain import LedgerEntryType
from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository
from tests.live.test_postgres_prepaid_charge_fence import DB_URL, seed
from tests.live.test_postgres_prepaid_charge_fence import database as _predecessor_fixture

predecessor_database = _predecessor_fixture

pytestmark = pytest.mark.skipif(not DB_URL, reason="explicit scratch loopback PostgreSQL required")
TEST_TELEGRAM_ID = 10101


def migration(number):
    path = next((Path(__file__).resolve().parents[2] / "alembic/versions").glob(f"{number}_*.py"))
    spec = importlib.util.spec_from_file_location(f"hourly_migration_{number}", path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
async def database(predecessor_database):
    supplemental = MetaData()
    # Foreign-key resolution metadata includes the existing users table, but
    # only the two new predecessor policy/audit tables are created here.
    UserRow.__table__.to_metadata(supplemental)
    tables = [
        model.__table__.to_metadata(supplemental)
        for model in (AuditEvent, TelegramSuperadminAssignment)
    ]
    for table in supplemental.tables.values():
        for column in table.columns:
            if column.server_default is None:
                continue
            default = str(column.server_default.arg)
            if "uuid_generate_v4" in default:
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif default == "CURRENT_TIMESTAMP":
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))

    def upgrade(connection):
        supplemental.create_all(connection, tables=tables)
        connection.execute(
            text(
                "CREATE FUNCTION uuid_generate_v4() RETURNS uuid "
                "LANGUAGE SQL AS $$ SELECT gen_random_uuid() $$"
            )
        )
        # Reconstruct only the additive predecessors inside this empty random
        # test schema, then execute the real upgrades (including real triggers).
        connection.execute(text("ALTER TABLE servers DROP COLUMN funding_source CASCADE"))
        connection.execute(text("ALTER TABLE servers DROP COLUMN funding_operator_id CASCADE"))
        connection.execute(
            text(
                "CREATE TABLE prepaid_hourly_periods ("
                "id uuid PRIMARY KEY DEFAULT uuid_generate_v4(), "
                "server_id uuid NOT NULL REFERENCES servers(id),"
                "wallet_id uuid NOT NULL REFERENCES wallets(id), period_start timestamp NOT NULL,"
                "period_end timestamp NOT NULL, usd_minor bigint NOT NULL, "
                "irt_minor bigint NOT NULL,"
                "fx_snapshot jsonb NOT NULL, idempotency_key varchar(255) NOT NULL UNIQUE,"
                "status varchar(16) NOT NULL, CONSTRAINT uq_prepaid_hourly_server_start "
                "UNIQUE(server_id,period_start), CONSTRAINT ck_prepaid_hourly_positive "
                "CHECK(usd_minor>0 AND irt_minor>0 AND period_end>period_start))"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration("0056").upgrade()
            migration("0058").upgrade()

    async with predecessor_database.begin() as session:
        connection = await session.connection()
        await connection.run_sync(upgrade)
    yield predecessor_database


async def corporate(database):
    owner, server_id = await seed(database)
    created = datetime.now(UTC) - timedelta(minutes=2)
    async with database.begin() as session:
        await session.execute(
            text("UPDATE users SET telegram_user_id=:telegram WHERE id=:id"),
            {"telegram": TEST_TELEGRAM_ID, "id": owner},
        )
        await session.execute(
            text(
                "UPDATE servers SET funding_source='operator',"
                "funding_operator_id=user_id,billing_model='hourly',provider_server_id='accepted-1',"
                "created_at=:created WHERE id=:id"
            ),
            {"created": created.replace(tzinfo=None), "id": server_id},
        )
    await SqlAlchemySuperadminRepository(database).reconcile([TEST_TELEGRAM_ID])
    row = CloudServer(
        id=server_id,
        user_id=owner,
        provider_key="test",
        provider_account_id=uuid4(),
        provider_server_id="accepted-1",
        state=ServerLifecycleState.RUNNING,
        billing_model="hourly",
        created_at=created,
        funding_source=FundingSource.OPERATOR,
        funding_operator_id=owner,
    )
    price = SimpleNamespace(
        selling_minor=25,
        selling_currency="USD",
        offer=SimpleNamespace(
            provider_rate_exact="0.123456789012345678901234567890123456789", currency="EUR"
        ),
    )
    periods = SqlAlchemyPrepaidHourlyPeriodRepository(database)
    funding = FundingService(
        SqlAlchemyOperatorFundingRepository(database, configured_ids=[TEST_TELEGRAM_ID])
    )
    forbidden = SimpleNamespace(get=AsyncMock(side_effect=AssertionError("personal wallet read")))
    service = PrepaidHourlyBillingService(
        server_repo=SimpleNamespace(),
        wallet_repo=forbidden,
        ledger_repo=forbidden,
        snapshot_repo=SimpleNamespace(get=AsyncMock(return_value=price)),
        period_repo=periods,
        fx_resolver=None,
        funding_service=funding,
    )
    return row, price, periods, funding, forbidden, service


async def money(database):
    async with database() as session:
        return (
            (await session.execute(select(WalletRow.balance))).scalar_one(),
            len((await session.execute(select(LedgerRow.id))).scalars().all()),
        )


async def test_real_operator_first_receipt_advances_coverage_without_wallet_or_fx(database):
    row, price, periods, _, forbidden, service = await corporate(database)
    before = await money(database)
    bound = await service.prepare_operator_first_hour(row, price)
    assert bound.wallet_id is bound.irt_minor is bound.fx_snapshot is None
    paid = await service.capture_operator_first_hour(row)
    assert paid.operator_expense_id is not None
    assert await service.capture_operator_first_hour(row) == paid
    async with database() as session:
        stored = (
            await session.execute(select(ServerRow).where(ServerRow.id == row.id))
        ).scalar_one()
        assert stored.prepaid_paid_until == paid.period_end.replace(tzinfo=None)
    assert await periods.list_charged_pending() == []
    assert await money(database) == before
    forbidden.get.assert_not_called()


async def test_real_paid_receipt_mismatch_cannot_advance_server_watermark(database):
    row, price, periods, _, _, service = await corporate(database)
    bound = await service.prepare_operator_first_hour(row, price)
    with pytest.raises(ValueError, match="coherent immutable expense"):
        await periods.mark_paid(replace(bound, operator_expense_id=uuid4()))
    async with database() as session:
        assert (
            await session.execute(
                select(ServerRow.prepaid_paid_until).where(ServerRow.id == row.id)
            )
        ).scalar_one() is None
    pending = await periods.get(row.id, row.created_at)
    assert pending.status.value == "pending" and pending.operator_expense_id is None


async def test_real_committed_expense_repairs_pending_coverage_after_policy_revocation(database):
    row, price, periods, funding, _, service = await corporate(database)
    bound = await service.prepare_operator_first_hour(row, price)
    grant = await funding.get_grant(bound.idempotency_key)
    expense = await funding.settle(grant)
    pending = await periods.list_charged_pending()
    assert len(pending) == 1 and pending[0].operator_expense_id is None
    await SqlAlchemySuperadminRepository(database).reconcile([])
    paid = await service.capture_operator_first_hour(row)
    assert paid.operator_expense_id == expense.id
    assert not await periods.list_charged_pending()


async def test_real_concurrent_corporate_settlement_creates_one_period_receipt(database):
    row, price, _, _, _, service = await corporate(database)
    await service.prepare_operator_first_hour(row, price)
    values = await asyncio.wait_for(
        asyncio.gather(*(service.capture_operator_first_hour(row) for _ in range(3))), timeout=8
    )
    assert len({value.operator_expense_id for value in values}) == 1
    async with database() as session:
        assert len((await session.execute(select(PeriodRow.id))).scalars().all()) == 1


async def test_real_stale_customer_debit_after_operator_cutover_posts_no_personal_money(database):
    row, _, _, _, _, _ = await corporate(database)
    before = await money(database)
    with pytest.raises(ValueError, match="customer"):
        await SqlAlchemyWalletRepository(database).adjust(
            row.user_id,
            -25,
            "stale-customer-key",
            entry_type=LedgerEntryType.CHARGE,
            reference_type="server",
            reference_id=str(row.id),
            description="stale accepted customer branch",
            expected_currency="IRT",
            expected_customer_server_id=row.id,
        )
    assert await money(database) == before
