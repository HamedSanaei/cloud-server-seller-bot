"""Calendar bounds, fresh RBAC and distinct exact financial report measures."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from test_admin_console_payments import ADMIN_TID, admin

from cloud_platform.modules.admin_console.report_repository import SqlAlchemyAdminReportRepository
from cloud_platform.modules.admin_console.reports import (
    AdminReportsService,
    FinancialTotals,
    report_window,
)
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, UserStatus

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


@pytest.mark.parametrize("fresh_change", ["blocked", "role", "identity", "deleted"])
async def test_report_refreshes_persisted_admin_before_any_financial_query(fresh_change):
    actor = admin()
    fresh = {
        "blocked": replace(actor, status=UserStatus.BANNED),
        "role": replace(actor, role=Role.USER),
        "identity": replace(actor, telegram_user_id=ADMIN_TID + 1),
        "deleted": None,
    }[fresh_change]
    repository = SimpleNamespace(summarize=AsyncMock())
    facade = AdminReportsService(
        SimpleNamespace(get=AsyncMock(return_value=fresh)),
        repository,
        configured_ids=(ADMIN_TID,),
        clock=lambda: NOW,
    )
    with pytest.raises(PermissionDeniedError):
        await facade.summary(actor)
    repository.summarize.assert_not_awaited()


@pytest.mark.parametrize(
    ("period", "anchor", "expected_start", "expected_end"),
    [
        ("week", NOW, datetime(2026, 10, 12, tzinfo=UTC), NOW),
        ("month", NOW, datetime(2026, 10, 1, tzinfo=UTC), NOW),
        (
            "month",
            datetime(2024, 2, 15, tzinfo=UTC),
            datetime(2024, 2, 1, tzinfo=UTC),
            datetime(2024, 3, 1, tzinfo=UTC),
        ),
        (
            "month",
            datetime(2025, 12, 5, tzinfo=UTC),
            datetime(2025, 12, 1, tzinfo=UTC),
            datetime(2026, 1, 1, tzinfo=UTC),
        ),
        (
            "week",
            datetime(2026, 10, 12, 1, tzinfo=timezone(timedelta(hours=3, minutes=30))),
            datetime(2026, 10, 5, tzinfo=UTC),
            datetime(2026, 10, 12, tzinfo=UTC),
        ),
    ],
)
async def test_calendar_periods_are_utc_bounded_and_current_period_is_to_date(
    period, anchor, expected_start, expected_end
):
    actor = admin()
    repo = SimpleNamespace(summarize=AsyncMock(return_value=FinancialTotals()))
    users = SimpleNamespace(get=AsyncMock(return_value=actor))
    facade = AdminReportsService(users, repo, configured_ids=(ADMIN_TID,), clock=lambda: NOW)
    result = await facade.summary(actor, period, anchor=anchor)
    assert result.start == expected_start and result.end == expected_end
    repo.summarize.assert_awaited_once_with(expected_start, expected_end)


@pytest.mark.parametrize(
    "period,anchor",
    [
        ("year", NOW),
        ("month", datetime(2026, 1, 1)),
        ("week", NOW + timedelta(days=1)),
    ],
)
def test_invalid_or_future_report_periods_are_rejected(period, anchor):
    with pytest.raises(ValueError):
        report_window(period, anchor, NOW)


async def test_repository_separates_cash_purchases_refunds_and_company_costs_exactly():
    statements = []
    results = [
        [("EUR", Decimal("12345"), 2), ("USD", Decimal("1050"), 1)],
        [("charge", "EUR", Decimal("700"), 4), ("refund", "EUR", Decimal("100"), 1)],
        [("IRT", Decimal("1500000"), 3)],
        [("USD", Decimal("0.123456789012345678901234567890123456789"), 3)],
        2,
    ]

    class DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt):
            statements.append(
                str(
                    stmt.compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )
            )
            value = results[len(statements) - 1]
            return SimpleNamespace(all=lambda: value, scalar_one=lambda: value)

    start = datetime(2026, 10, 1, tzinfo=UTC)
    report = await SqlAlchemyAdminReportRepository(DB).summarize(start, NOW)
    assert len(statements) == 5
    assert [(row.currency, row.amount_minor) for row in report.payment_receipts] == [
        ("EUR", 12345),
        ("USD", 1050),
    ]
    assert report.customer_charges[0].amount_minor == 700
    assert report.customer_refunds[0].amount_minor == 100
    assert report.net_customer_revenue[0].amount_minor == 600
    assert report.corporate_nominal_expenses[0].amount_minor == 1500000
    assert report.corporate_provider_costs[0].amount_exact == (
        "0.123456789012345678901234567890123456789"
    )
    assert report.completed_customer_monthly_orders == 2
    assert "credited_at >=" in statements[0] and "status = 'succeeded'" in statements[0]
    assert "users" not in statements[0] + statements[1]
    assert "operator_expenses" not in statements[0] + statements[1]
    assert "accrual_periods" not in statements[1] and "provider_orders" not in statements[1]
    assert "CAST(" in statements[3] and "NUMERIC" in statements[3]
    assert "funding_source = 'customer_wallet'" in statements[4]
    assert "settlement_status = 'complete'" in statements[4]
    assert "settlement_attempted_at >=" in statements[4]
    assert all(" < '2026-10-16" in statement for statement in statements)


@pytest.mark.parametrize(
    "start,end",
    [
        (datetime(2026, 1, 1), NOW),
        (NOW, NOW - timedelta(days=1)),
        (NOW - timedelta(days=33), NOW),
    ],
)
async def test_repository_refuses_unbounded_periods_before_opening_session(start, end):
    factory = AsyncMock()
    with pytest.raises(ValueError):
        await SqlAlchemyAdminReportRepository(factory).summarize(start, end)
    factory.assert_not_called()


async def test_actual_queries_keep_historical_totals_after_role_change_and_never_add_costs():
    """SQLite executes the production predicates; exact PG decimals have a separate vector."""
    from uuid import uuid4

    from sqlalchemy import (
        JSON,
        BigInteger,
        Column,
        DateTime,
        MetaData,
        String,
        Table,
        Uuid,
        create_engine,
        insert,
        update,
    )

    metadata = MetaData()
    users = Table("users", metadata, Column("id", Uuid, primary_key=True), Column("role", String))
    wallets = Table(
        "wallets", metadata, Column("id", Uuid, primary_key=True), Column("user_id", Uuid)
    )
    payments = Table(
        "payment_sessions",
        metadata,
        Column("user_id", Uuid),
        Column("currency", String),
        Column("amount_minor", BigInteger),
        Column("status", String),
        Column("credited_at", DateTime),
    )
    ledger = Table(
        "ledger",
        metadata,
        Column("wallet_id", Uuid),
        Column("currency", String),
        Column("amount", BigInteger),
        Column("entry_type", String),
        Column("created_at", DateTime),
    )
    expenses = Table(
        "operator_expenses",
        metadata,
        Column("nominal_currency", String),
        Column("nominal_amount_minor", BigInteger),
        Column("provider_cost_snapshot", JSON),
        Column("occurred_at", DateTime(timezone=True)),
    )
    orders = Table(
        "provider_orders",
        metadata,
        Column("funding_source", String),
        Column("status", String),
        Column("settlement_status", String),
        Column("settlement_attempted_at", DateTime(timezone=True)),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    owner, wallet_id = uuid4(), uuid4()
    occurred = NOW - timedelta(days=1)
    with engine.begin() as db:
        db.execute(insert(users), [{"id": owner, "role": "user"}])
        db.execute(insert(wallets), [{"id": wallet_id, "user_id": owner}])
        db.execute(
            insert(payments),
            [
                {
                    "user_id": owner,
                    "currency": "USD",
                    "amount_minor": 1050,
                    "status": "succeeded",
                    "credited_at": occurred.replace(tzinfo=None),
                }
            ],
        )
        db.execute(
            insert(ledger),
            [
                {
                    "wallet_id": wallet_id,
                    "currency": "EUR",
                    "amount": amount,
                    "entry_type": kind,
                    "created_at": occurred.replace(tzinfo=None),
                }
                for kind, amount in [
                    ("charge", 700),
                    ("refund", 100),
                    ("deposit", 100000),
                    ("adjustment", 200000),
                ]
            ],
        )
        db.execute(
            insert(expenses),
            [
                {
                    "nominal_currency": "IRT",
                    "nominal_amount_minor": 999999,
                    "provider_cost_snapshot": {"currency": "USD", "amount_exact": "0.125"},
                    "occurred_at": occurred,
                }
            ],
        )
        db.execute(
            insert(orders),
            [
                {
                    "funding_source": source,
                    "status": "active",
                    "settlement_status": "complete",
                    "settlement_attempted_at": occurred,
                }
                for source in ["customer_wallet", "operator"]
            ],
        )

    class DB:
        async def __aenter__(self):
            self.connection = engine.connect()
            return self

        async def __aexit__(self, *args):
            self.connection.close()

        async def execute(self, statement):
            return self.connection.execute(statement)

    try:
        repo = SqlAlchemyAdminReportRepository(DB)
        start = NOW.replace(day=1, hour=0)
        original = await repo.summarize(start, NOW)
        for role in ["admin", "user"]:
            with engine.begin() as db:
                db.execute(update(users).where(users.c.id == owner).values(role=role))
            assert await repo.summarize(start, NOW) == original
        assert original.payment_receipts[0].amount_minor == 1050
        assert original.customer_charges[0].amount_minor == 700
        assert original.customer_refunds[0].amount_minor == 100
        assert original.net_customer_revenue[0].amount_minor == 600
        assert original.corporate_nominal_expenses[0].amount_minor == 999999
        assert original.corporate_provider_costs[0].amount_exact == "0.125"
        assert original.completed_customer_monthly_orders == 1
    finally:
        engine.dispose()
