"""Opt-in durable prepaid-hour coverage proofs on real, migrated PostgreSQL.

Only the explicit scratch CLOUD_PLATFORM_TEST_POSTGRES_URL is accepted. Each
migration path owns a random schema; each test owns and deletes its own rows.
No application settings, public-table truncation, provider calls, or live FX
requests are used. Database constraints, competing transactions, immutable
receipts, and naive-UTC timestamp bindings are exercised rather than mocked.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cloud_platform.db.base import LedgerEntry as LedgerRow
from cloud_platform.db.base import PrepaidHourlyPeriod as PeriodRow
from cloud_platform.db.base import Provider as ProviderRow
from cloud_platform.db.base import ProviderAccount as AccountRow
from cloud_platform.db.base import Server as ServerRow
from cloud_platform.db.base import ServerPriceSnapshot as SnapshotRow
from cloud_platform.db.base import User as UserRow
from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.db.timestamps import to_db_utc
from cloud_platform.modules.billing.repository import (
    PostgresAdvisoryAccrualLock,
    SqlAlchemyPrepaidHourlyPeriodRepository,
)
from cloud_platform.modules.billing.service import (
    PrepaidHourlyBillingService,
    PrepaidHourlyPeriod,
    PrepaidHourlyPeriodStatus,
    prepaid_hourly_key,
)
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
from cloud_platform.modules.fx.domain import ConversionSnapshot, FxPurpose
from cloud_platform.modules.notifications.hourly import HourlyRunwayWarningService
from cloud_platform.modules.notifications.repository import (
    SqlAlchemyLowBalanceNotificationLogRepository,
)
from cloud_platform.modules.pricing.domain import MarginRule, OfferCost, ServerPriceSnapshot
from cloud_platform.modules.pricing.repository import SqlAlchemyServerPriceSnapshotRepository
from cloud_platform.modules.wallet.repository import (
    HoldService,
    SqlAlchemyHoldRepository,
    SqlAlchemyLedgerRepository,
    SqlAlchemyWalletRepository,
)

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
ROOT = Path(__file__).resolve().parents[2]
ANCHOR = datetime(2026, 9, 14, 10, 17, 23, 456789, tzinfo=UTC)
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="requires explicit scratch CLOUD_PLATFORM_TEST_POSTGRES_URL"
)


def _migrate_schema(schema: str, start: str) -> None:
    """Run real Alembic revisions outside the local alembic package's cwd."""
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    engine = sa.create_engine(
        make_url(DB_URL).set(drivername="postgresql"),
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={"options": f"-csearch_path={schema},public -ctimezone=UTC"},
    )
    try:
        with engine.begin() as connection:

            def upgrade(target: str) -> None:
                context = MigrationContext.configure(
                    connection,
                    opts={
                        "version_table_schema": schema,
                        "fn": lambda revisions, _context: scripts._upgrade_revs(target, revisions),
                    },
                )
                with Operations.context(context):
                    context.run_migrations()

            if start == "historical0050":
                upgrade("0050")
                user_id, wallet_id = uuid4(), uuid4()
                connection.execute(
                    sa.text("INSERT INTO users (id, username, email) VALUES (:id, :name, :email)"),
                    {"id": user_id, "name": schema, "email": f"{schema}@example.test"},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO wallets (id, user_id, balance, currency) "
                        "VALUES (:id, :user_id, 731415, 'IRT')"
                    ),
                    {"id": wallet_id, "user_id": user_id},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO ledger (id, wallet_id, amount, currency, entry_type, "
                        "idempotency_key) VALUES (:id, :wallet_id, 178, 'IRT', 'charge', :key)"
                    ),
                    {"id": uuid4(), "wallet_id": wallet_id, "key": schema},
                )
            upgrade("head")
            assert (
                connection.execute(
                    sa.text(f'SELECT version_num FROM "{schema}".alembic_version')
                ).scalar_one()
                == scripts.get_current_head()
            )
            if start == "historical0050":
                assert connection.execute(
                    sa.text("SELECT balance, currency FROM wallets WHERE id = :id"),
                    {"id": wallet_id},
                ).one() == (731415, "IRT")
                assert connection.execute(
                    sa.text("SELECT amount, currency FROM ledger WHERE wallet_id = :id"),
                    {"id": wallet_id},
                ).one() == (178, "IRT")
    finally:
        engine.dispose()


@pytest.fixture(scope="module", params=["fresh", "historical0050"])
def migrated_schema(request, tmp_path_factory) -> Iterator[str]:
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("prepaid coverage tests require explicit loopback scratch PostgreSQL")
    schema = f"prepaid_coverage_{uuid4().hex}"
    engine = sa.create_engine(
        url.set(drivername="postgresql"), poolclass=NullPool, hide_parameters=True
    )
    created = False
    try:
        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            created = True
        command = (
            "import runpy, sys; namespace = runpy.run_path(sys.argv[1]); "
            "namespace['_migrate_schema'](sys.argv[2], sys.argv[3])"
        )
        result = subprocess.run(
            [sys.executable, "-c", command, str(Path(__file__).resolve()), schema, request.param],
            cwd=tmp_path_factory.mktemp("prepaid-migrations"),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        yield schema
    finally:
        try:
            if created:
                with engine.begin() as connection:
                    connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            engine.dispose()


@dataclass
class Database:
    factory: async_sessionmaker[AsyncSession]
    user_ids: list[UUID] = field(default_factory=list)
    provider_ids: list[UUID] = field(default_factory=list)


@dataclass(frozen=True)
class Contract:
    server: CloudServer
    wallet_id: UUID
    balance: int


@pytest.fixture
async def database(migrated_schema: str) -> AsyncIterator[Database]:
    engine = create_async_engine(
        DB_URL,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "search_path": f"{migrated_schema},public",
                "timezone": "UTC",
                "statement_timeout": "15000",
                "lock_timeout": "5000",
            }
        },
    )
    database = Database(async_sessionmaker(engine, expire_on_commit=False))
    try:
        yield database
    finally:
        try:
            if database.user_ids:
                async with database.factory.begin() as session:
                    owned_servers = sa.select(ServerRow.id).where(
                        ServerRow.user_id.in_(database.user_ids)
                    )
                    await session.execute(
                        sa.delete(SnapshotRow).where(SnapshotRow.server_id.in_(owned_servers))
                    )
                    await session.execute(
                        sa.delete(UserRow).where(UserRow.id.in_(database.user_ids))
                    )
                    await session.execute(
                        sa.delete(ProviderRow).where(ProviderRow.id.in_(database.provider_ids))
                    )
        finally:
            await engine.dispose()


async def seed_contract(
    database: Database,
    *,
    balance: int = 1_000_000,
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
) -> Contract:
    user_id, wallet_id, provider_id, account_id, server_id = (uuid4() for _ in range(5))
    database.user_ids.append(user_id)
    database.provider_ids.append(provider_id)
    provider_key = f"prepaid-{provider_id.hex}"
    async with database.factory.begin() as session:
        session.add(UserRow(id=user_id, username=user_id.hex, email=f"{user_id.hex}@example.test"))
        session.add(ProviderRow(id=provider_id, name=provider_key))
        await session.flush()
        session.add(WalletRow(id=wallet_id, user_id=user_id, balance=balance, currency="IRT"))
        session.add(AccountRow(id=account_id, user_id=user_id, provider_id=provider_id))
        await session.flush()
        session.add(
            ServerRow(
                id=server_id,
                user_id=user_id,
                provider_id=provider_id,
                provider_account_id=account_id,
                state=state.value,
                billing_model="hourly_prepaid_irt",
                price_per_quantum=100,
                currency="USD",
                idempotency_key=f"prepaid-create:{server_id}",
                created_at=to_db_utc(ANCHOR),
            )
        )
    await SqlAlchemyServerPriceSnapshotRepository(database.factory).create(
        ServerPriceSnapshot(
            server_id=server_id,
            offer=OfferCost(
                provider_key=provider_key,
                plan_id="offline-plan",
                location_id="offline-location",
                cost_minor=100,
                currency="USD",
                provider_rate_exact="1.00",
            ),
            selling_minor=250,
            selling_currency="USD",
            book_name="prepaid-test",
            book_version=1,
            rule=MarginRule(provider="*", plan="*", location="*", margin_factor=Decimal("2.5")),
            priced_at=ANCHOR,
        )
    )
    server = await SqlAlchemyServerRepository(database.factory).get(server_id)
    assert server is not None and server.created_at == ANCHOR
    return Contract(server, wallet_id, balance)


def quote(usd_minor: int = 250, rate: int = 100_000) -> ConversionSnapshot:
    observed = datetime.now(UTC) - timedelta(seconds=1)
    return ConversionSnapshot(
        source_amount_minor=usd_minor,
        source_currency="USD",
        target_amount_minor=usd_minor * rate // 100,
        target_currency="IRT",
        rate=Decimal(rate),
        purpose=FxPurpose.CHARGE,
        source="abantether",
        path="USDTIRT.buy (proxy USDT for USD)",
        observed_at=observed,
        expires_at=observed + timedelta(minutes=5),
        proxy=True,
        proxy_asset="USDT",
    )


def period(
    contract: Contract,
    start: datetime = ANCHOR,
    *,
    usd_minor: int = 250,
    rate: int = 100_000,
) -> PrepaidHourlyPeriod:
    snapshot = quote(usd_minor, rate)
    return PrepaidHourlyPeriod(
        server_id=contract.server.id,
        wallet_id=contract.wallet_id,
        period_start=start,
        period_end=start + timedelta(hours=1),
        usd_minor=usd_minor,
        irt_minor=snapshot.target_amount_minor,
        fx_snapshot=snapshot,
        idempotency_key=prepaid_hourly_key(contract.server.id, start),
    )


async def persisted(database: Database, contract: Contract) -> tuple[datetime | None, int, int]:
    async with database.factory() as session:
        watermark = await session.scalar(
            sa.select(ServerRow.prepaid_paid_until).where(ServerRow.id == contract.server.id)
        )
        balance = await session.scalar(
            sa.select(WalletRow.balance).where(WalletRow.id == contract.wallet_id)
        )
        receipts = await session.scalar(
            sa.select(sa.func.count())
            .select_from(LedgerRow)
            .where(LedgerRow.wallet_id == contract.wallet_id, LedgerRow.entry_type == "charge")
        )
        return watermark, int(balance), int(receipts)


async def test_equivalent_offset_bind_get_and_paid_replay_store_naive_utc(
    database: Database,
) -> None:
    contract = await seed_contract(database)
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    local = ANCHOR.astimezone(timezone(timedelta(hours=3, minutes=30)))
    requested = period(contract, local)
    assert requested.period_start.tzinfo is UTC
    assert requested.period_start == ANCHOR
    bound = await repository.bind(requested)
    assert bound.id is not None and bound.fx_snapshot.to_dict() == requested.fx_snapshot.to_dict()
    assert await repository.get(contract.server.id, local) == bound
    assert await repository.bind(requested) == bound
    await repository.mark_paid(requested)
    await repository.mark_paid(requested)
    paid = await repository.get(contract.server.id, ANCHOR)
    assert paid is not None and paid.status is PrepaidHourlyPeriodStatus.PAID
    async with database.factory() as session:
        raw = (
            await session.execute(
                sa.select(PeriodRow.period_start, PeriodRow.period_end).where(
                    PeriodRow.id == bound.id
                )
            )
        ).one()
        assert raw == (to_db_utc(ANCHOR), to_db_utc(ANCHOR + timedelta(hours=1)))
        assert raw.period_start.tzinfo is None and raw.period_end.tzinfo is None
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(PeriodRow)
                .where(PeriodRow.server_id == contract.server.id)
            )
            == 1
        )
    assert await persisted(database, contract) == (
        to_db_utc(ANCHOR + timedelta(hours=1)),
        contract.balance,
        0,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.get(contract.server.id, to_db_utc(ANCHOR))


class RacingRepository(SqlAlchemyPrepaidHourlyPeriodRepository):
    """Synchronize real absent-row reads, never replace SQL or constraint checks."""

    def __init__(self, factory, barrier: asyncio.Barrier) -> None:
        super().__init__(factory)
        self.barrier = barrier
        self.first_read = True

    async def get(self, server_id: UUID, start: datetime) -> PrepaidHourlyPeriod | None:
        row = await super().get(server_id, start)
        if self.first_read:
            self.first_read = False
            assert row is None
            await asyncio.wait_for(self.barrier.wait(), timeout=5)
        return row


@pytest.mark.parametrize(
    "changed_usd", [False, True], ids=["different-fresh-fx", "conflicting-usd"]
)
async def test_concurrent_bind_uses_one_committed_quote(
    database: Database, changed_usd: bool
) -> None:
    contract = await seed_contract(database)
    barrier = asyncio.Barrier(2)
    candidates = [
        period(contract),
        period(contract, usd_minor=251 if changed_usd else 250, rate=120_000),
    ]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                RacingRepository(database.factory, barrier).bind(candidate)
                for candidate in candidates
            ),
            return_exceptions=True,
        ),
        timeout=10,
    )
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    committed = await repository.get(contract.server.id, ANCHOR)
    assert committed is not None
    if changed_usd:
        failures = [result for result in results if isinstance(result, BaseException)]
        assert len(failures) == 1 and isinstance(failures[0], ValueError)
        assert "conflicting prepaid hourly binding" in str(failures[0])
        assert committed == next(
            result for result in results if isinstance(result, PrepaidHourlyPeriod)
        )
    else:
        assert results == [committed, committed]
        assert committed.fx_snapshot in [candidate.fx_snapshot for candidate in candidates]
        assert committed.irt_minor in {250_000, 300_000}
    async with database.factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(PeriodRow)
                .where(PeriodRow.server_id == contract.server.id)
            )
            == 1
        )
    assert await persisted(database, contract) == (None, contract.balance, 0)


@pytest.mark.parametrize("fact", ["wallet", "usd", "key", "end"])
async def test_binding_conflicts_fail_closed_without_moving_money(
    database: Database, fact: str
) -> None:
    contract = await seed_contract(database)
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    bound = await repository.bind(period(contract))
    if fact == "wallet":
        other = await seed_contract(database)
        candidate = replace(bound, wallet_id=other.wallet_id)
        with pytest.raises(ValueError, match="conflicting prepaid hourly binding"):
            await repository.bind(candidate)
    elif fact == "usd":
        with pytest.raises(ValueError, match="conflicting prepaid hourly binding"):
            await repository.bind(period(contract, usd_minor=251))
    else:
        # Older/corrupt persisted facts must not bypass the domain validation on read.
        changes = (
            {"idempotency_key": "corrupt-key"}
            if fact == "key"
            else {"period_end": to_db_utc(bound.period_end + timedelta(minutes=1))}
        )
        async with database.factory.begin() as session:
            await session.execute(
                sa.update(PeriodRow).where(PeriodRow.id == bound.id).values(**changes)
            )
        with pytest.raises(ValueError):
            await repository.bind(period(contract))
    assert await persisted(database, contract) == (None, contract.balance, 0)


@pytest.mark.parametrize(
    "corruption, message",
    [
        ("wrong_first_start", "first paid hour"),
        ("gap", "contiguous"),
        ("pending_at_end", "contiguous"),
        ("already_paid_without_watermark", "first paid hour"),
        ("already_paid_before_watermark", "paid period cannot precede"),
        ("changed_fx", "changed FX binding"),
        ("missing_period", "changed FX binding"),
    ],
)
async def test_corrupted_coverage_or_changed_fx_cannot_advance(
    database: Database, corruption: str, message: str
) -> None:
    contract = await seed_contract(database)
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    start = ANCHOR + timedelta(hours=1) if corruption == "wrong_first_start" else ANCHOR
    requested = period(contract, start)
    if corruption != "missing_period":
        await repository.bind(requested)
    watermark = None
    if corruption == "gap":
        watermark = ANCHOR + timedelta(minutes=30)
    elif corruption == "pending_at_end":
        watermark = requested.period_end
    elif corruption == "already_paid_before_watermark":
        watermark = requested.period_start
    async with database.factory.begin() as session:
        await session.execute(
            sa.update(ServerRow)
            .where(ServerRow.id == contract.server.id)
            .values(prepaid_paid_until=to_db_utc(watermark) if watermark is not None else None)
        )
        if corruption.startswith("already_paid"):
            await session.execute(
                sa.update(PeriodRow)
                .where(PeriodRow.server_id == contract.server.id)
                .values(status=PrepaidHourlyPeriodStatus.PAID.value)
            )
    if corruption == "changed_fx":
        requested = period(contract, rate=120_000)
    before = await persisted(database, contract)
    with pytest.raises(ValueError, match=message):
        await repository.mark_paid(requested)
    assert await persisted(database, contract) == before
    if corruption not in {
        "missing_period",
        "already_paid_without_watermark",
        "already_paid_before_watermark",
    }:
        stored = await repository.get(contract.server.id, start)
        assert stored is not None and stored.status is PrepaidHourlyPeriodStatus.PENDING


async def _wait_for_pg_blocker(
    database: Database, pid: int, blocker_pid: int, peer_pid: int
) -> None:
    async with asyncio.timeout(3):
        while True:
            async with database.factory() as session:
                blocked = await session.scalar(
                    sa.text(
                        "SELECT :blocker = ANY(pg_blocking_pids(CAST(:pid AS integer))) "
                        "OR :peer = ANY(pg_blocking_pids(CAST(:pid AS integer)))"
                    ),
                    {"pid": pid, "blocker": blocker_pid, "peer": peer_pid},
                )
            if blocked:
                return
            await asyncio.sleep(0.01)


async def test_paid_transactions_wait_for_real_row_lock_and_never_regress(
    database: Database,
) -> None:
    contract = await seed_contract(database)
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    first = await repository.bind(period(contract))
    second = await repository.bind(period(contract, ANCHOR + timedelta(hours=1)))
    pids: asyncio.Queue[int] = asyncio.Queue()

    @asynccontextmanager
    async def observed_factory() -> AsyncIterator[AsyncSession]:
        async with database.factory() as session:
            pid = await session.scalar(sa.text("SELECT pg_backend_pid()"))
            await pids.put(pid)
            yield session

    tasks: list[asyncio.Task[None]] = []
    async with database.factory() as blocker:
        await blocker.begin()
        blocker_pid = await blocker.scalar(sa.text("SELECT pg_backend_pid()"))
        await blocker.execute(
            sa.select(ServerRow).where(ServerRow.id == contract.server.id).with_for_update()
        )
        try:
            locked_repository = SqlAlchemyPrepaidHourlyPeriodRepository(observed_factory)
            tasks = [asyncio.create_task(locked_repository.mark_paid(first)) for _ in range(2)]
            worker_pids = [await asyncio.wait_for(pids.get(), timeout=3) for _ in range(2)]
            for pid in worker_pids:
                peer_pid = next(other for other in worker_pids if other != pid)
                await _wait_for_pg_blocker(database, pid, blocker_pid, peer_pid)
            assert all(not task.done() for task in tasks)
            await blocker.commit()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        finally:
            await blocker.rollback()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    await repository.mark_paid(second)
    await repository.mark_paid(second)
    with pytest.raises(ValueError, match="contiguous"):
        await repository.mark_paid(first)
    assert await persisted(database, contract) == (
        to_db_utc(second.period_end),
        contract.balance,
        0,
    )
    async with database.factory() as session:
        statuses = (
            (
                await session.execute(
                    sa.select(PeriodRow.status).where(PeriodRow.server_id == contract.server.id)
                )
            )
            .scalars()
            .all()
        )
        assert statuses == ["paid", "paid"]


@pytest.mark.parametrize(
    "constraint",
    [
        "duplicate_start",
        "duplicate_key",
        "zero_usd",
        "zero_irt",
        "empty_period",
        "missing_wallet",
        "missing_server",
    ],
)
async def test_migrated_database_rejects_invalid_period_rows(
    database: Database, constraint: str
) -> None:
    contract = await seed_contract(database)
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    bound = await repository.bind(period(contract))
    values = {
        "id": uuid4(),
        "server_id": bound.server_id,
        "wallet_id": bound.wallet_id,
        "period_start": to_db_utc(bound.period_start + timedelta(hours=1)),
        "period_end": to_db_utc(bound.period_end + timedelta(hours=1)),
        "usd_minor": bound.usd_minor,
        "irt_minor": bound.irt_minor,
        "fx_snapshot": bound.fx_snapshot.to_dict(),
        "idempotency_key": f"raw-constraint:{uuid4()}",
        "status": "pending",
    }
    if constraint == "duplicate_start":
        values["period_start"] = to_db_utc(bound.period_start)
    elif constraint == "duplicate_key":
        values["idempotency_key"] = bound.idempotency_key
    elif constraint == "zero_usd":
        values["usd_minor"] = 0
    elif constraint == "zero_irt":
        values["irt_minor"] = 0
    elif constraint == "empty_period":
        values["period_end"] = values["period_start"]
    elif constraint == "missing_wallet":
        values["wallet_id"] = uuid4()
    else:
        values["server_id"] = uuid4()
    async with database.factory() as session:
        session.add(PeriodRow(**values))
        with pytest.raises(IntegrityError) as error:
            await session.commit()
        await session.rollback()
    expected_sqlstate = (
        "23505"
        if constraint.startswith("duplicate")
        else ("23503" if constraint.startswith("missing") else "23514")
    )
    assert error.value.orig.sqlstate == expected_sqlstate
    assert await repository.get(contract.server.id, ANCHOR) == bound
    async with database.factory() as session:
        assert (
            await session.scalar(
                sa.select(sa.func.count())
                .select_from(PeriodRow)
                .where(PeriodRow.server_id == contract.server.id)
            )
            == 1
        )
    assert await persisted(database, contract) == (None, contract.balance, 0)


async def test_marker_cas_and_lifecycle_save_preserve_the_newest_episode(
    database: Database,
) -> None:
    contract = await seed_contract(database)
    periods = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    first = await periods.bind(period(contract))
    await periods.mark_paid(first)
    servers = SqlAlchemyServerRepository(database.factory)
    stale = await servers.get(contract.server.id)
    assert stale is not None
    offset = timezone(timedelta(hours=3, minutes=30))
    episodes = [
        (ANCHOR.astimezone(offset), (ANCHOR + timedelta(minutes=5)).astimezone(offset)),
        (ANCHOR + timedelta(minutes=1), ANCHOR + timedelta(minutes=6)),
    ]
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                servers.save_prepaid_balance_markers(
                    contract.server.id,
                    expected_warning_since=None,
                    expected_zero_since=None,
                    warning_since=warning,
                    zero_since=zero,
                )
                for warning, zero in episodes
            ),
            return_exceptions=True,
        ),
        timeout=10,
    )
    assert sum(result is None for result in results) == 1
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(failures) == 1 and isinstance(failures[0], ValueError)
    assert "changed concurrently" in str(failures[0])
    winner = episodes[results.index(None)]
    current = await servers.get(contract.server.id)
    assert current is not None
    assert (current.low_balance_since, current.prepaid_zero_since) == winner
    await servers.save_prepaid_balance_markers(
        contract.server.id,
        expected_warning_since=winner[0],
        expected_zero_since=winner[1],
        warning_since=None,
        zero_since=None,
    )
    newer = (ANCHOR + timedelta(hours=3), ANCHOR + timedelta(hours=4))
    await servers.save_prepaid_balance_markers(
        contract.server.id,
        expected_warning_since=None,
        expected_zero_since=None,
        warning_since=newer[0],
        zero_since=newer[1],
    )
    with pytest.raises(ValueError, match="changed concurrently"):
        await servers.save_prepaid_balance_markers(
            contract.server.id,
            expected_warning_since=winner[0],
            expected_zero_since=winner[1],
            warning_since=None,
            zero_since=None,
        )
    stale.state = ServerLifecycleState.STOPPED
    stale.prepaid_paid_until = ANCHOR
    saved = await servers.save(stale)
    assert saved.state is ServerLifecycleState.STOPPED
    assert (saved.low_balance_since, saved.prepaid_zero_since) == newer
    assert saved.prepaid_paid_until == first.period_end
    async with database.factory() as session:
        raw = (
            await session.execute(
                sa.select(ServerRow.low_balance_since, ServerRow.prepaid_zero_since).where(
                    ServerRow.id == contract.server.id
                )
            )
        ).one()
        assert raw.low_balance_since.utcoffset() == timedelta(0)
        assert raw.prepaid_zero_since == to_db_utc(newer[1])
    assert await persisted(database, contract) == (to_db_utc(first.period_end), contract.balance, 0)


@pytest.mark.parametrize("contract_change", ["missing", "billing_model", "state"])
async def test_marker_cas_rejects_ineligible_server(
    database: Database, contract_change: str
) -> None:
    contract = await seed_contract(database)
    server_id = contract.server.id
    if contract_change == "missing":
        server_id = uuid4()
    else:
        change = (
            {"billing_model": "hourly"}
            if contract_change == "billing_model"
            else {"state": "deleted"}
        )
        async with database.factory.begin() as session:
            await session.execute(
                sa.update(ServerRow).where(ServerRow.id == server_id).values(**change)
            )
    with pytest.raises(ValueError):
        await SqlAlchemyServerRepository(database.factory).save_prepaid_balance_markers(
            server_id,
            expected_warning_since=None,
            expected_zero_since=None,
            warning_since=ANCHOR,
            zero_since=ANCHOR,
        )
    async with database.factory() as session:
        assert (
            await session.execute(
                sa.select(ServerRow.low_balance_since, ServerRow.prepaid_zero_since).where(
                    ServerRow.id == contract.server.id
                )
            )
        ).one() == (None, None)


async def test_charged_pending_selects_exact_wallet_key_even_for_corrupt_receipts(
    database: Database,
) -> None:
    first = await seed_contract(database, state=ServerLifecycleState.STOPPED)
    second = await seed_contract(database)
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    charged = await repository.bind(period(first))
    later = await repository.bind(period(first, ANCHOR + timedelta(hours=1)))
    uncharged = await repository.bind(period(first, ANCHOR + timedelta(hours=2)))
    other = await repository.bind(period(second))
    paid = await repository.bind(period(second, ANCHOR + timedelta(hours=1)))
    async with database.factory.begin() as session:
        await session.execute(
            sa.update(PeriodRow).where(PeriodRow.id == paid.id).values(status="paid")
        )
        for wallet_id, key in (
            (charged.wallet_id, charged.idempotency_key),
            (later.wallet_id, later.idempotency_key),
            (other.wallet_id, other.idempotency_key),
            (paid.wallet_id, paid.idempotency_key),
            # A same-key receipt in a different wallet is NOT payment evidence.
            (second.wallet_id, uncharged.idempotency_key),
            (first.wallet_id, f"unrelated:{uuid4()}"),
        ):
            session.add(
                LedgerRow(
                    id=uuid4(),
                    wallet_id=wallet_id,
                    idempotency_key=key,
                    amount=1,
                    currency="USD",
                    entry_type="deposit",
                    description="deliberately conflicting receipt: service must validate",
                )
            )
    expected = sorted(
        [charged, later, other], key=lambda row: (row.server_id.int, row.period_start)
    )
    assert await repository.list_charged_pending() == expected
    stopped = await SqlAlchemyServerRepository(database.factory).get(first.server.id)
    assert stopped is not None and stopped.state is ServerLifecycleState.STOPPED
    assert await repository.get(first.server.id, uncharged.period_start) == uncharged


class OfflineFx:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.calls = 0
        self.unavailable = unavailable

    async def snapshot(
        self, amount: int, source: str, target: str, purpose: FxPurpose
    ) -> ConversionSnapshot:
        self.calls += 1
        assert (source, target, purpose) == ("USD", "IRT", FxPurpose.CHARGE)
        if self.unavailable:
            raise RuntimeError("offline FX must not be consulted for a frozen receipt")
        return quote(amount)


def billing_service(database: Database, fx: OfflineFx, periods=None) -> PrepaidHourlyBillingService:
    return PrepaidHourlyBillingService(
        server_repo=SqlAlchemyServerRepository(database.factory),
        wallet_repo=SqlAlchemyWalletRepository(database.factory),
        ledger_repo=SqlAlchemyLedgerRepository(database.factory),
        snapshot_repo=SqlAlchemyServerPriceSnapshotRepository(database.factory),
        period_repo=periods or SqlAlchemyPrepaidHourlyPeriodRepository(database.factory),
        fx_resolver=fx,
    )


async def capture_first_hour(database: Database, contract: Contract) -> PrepaidHourlyPeriod:
    service = billing_service(database, OfflineFx())
    first = await service.prepare_first_hour(contract.server, contract.wallet_id, ANCHOR, 250)
    wallets = SqlAlchemyWalletRepository(database.factory)
    ledger = SqlAlchemyLedgerRepository(database.factory)
    holds = SqlAlchemyHoldRepository(database.factory)
    hold_service = HoldService(wallets, holds, ledger)
    await hold_service.create_hold(
        contract.wallet_id,
        first.irt_minor,
        "IRT",
        f"server-create:{contract.server.idempotency_key}",
    )
    await service.capture_first_hour(contract.server, holds, hold_service)
    await service.capture_first_hour(contract.server, holds, hold_service)
    assert await persisted(database, contract) == (
        to_db_utc(first.period_end),
        contract.balance - first.irt_minor,
        1,
    )
    return first


class InterruptCoverageWrite(SqlAlchemyPrepaidHourlyPeriodRepository):
    async def mark_paid(self, bound: PrepaidHourlyPeriod) -> None:
        raise RuntimeError("fault after durable ledger commit, before coverage transaction")


async def test_real_charge_after_coverage_write_failure_repairs_stopped_server_without_fx(
    database: Database,
) -> None:
    contract = await seed_contract(database)
    first = await capture_first_hour(database, contract)

    fx = OfflineFx()
    service = billing_service(database, fx, InterruptCoverageWrite(database.factory))
    failed = await service.run(first.period_end)
    assert failed.stop_requested_server_ids == [contract.server.id]
    assert "after durable ledger commit" in failed.errors[contract.server.id]
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    renewal = await repository.get(contract.server.id, first.period_end)
    assert renewal is not None and renewal.status is PrepaidHourlyPeriodStatus.PENDING
    assert await persisted(database, contract) == (
        to_db_utc(first.period_end),
        contract.balance - first.irt_minor - renewal.irt_minor,
        2,
    )
    servers = SqlAlchemyServerRepository(database.factory)
    stopped = await servers.get(contract.server.id)
    assert stopped is not None
    stopped.state = ServerLifecycleState.STOPPED
    await servers.save(stopped)
    unavailable_fx = OfflineFx(unavailable=True)
    recovered = await billing_service(database, unavailable_fx).run(first.period_end)
    assert recovered.errors == {} and recovered.stop_requested_server_ids == []
    assert recovered.replayed == 1 and recovered.charged == 0
    repaired = await repository.get(contract.server.id, first.period_end)
    assert repaired is not None and repaired.status is PrepaidHourlyPeriodStatus.PAID
    assert repaired.fx_snapshot == renewal.fx_snapshot
    assert await persisted(database, contract) == (
        to_db_utc(renewal.period_end),
        contract.balance - first.irt_minor - renewal.irt_minor,
        2,
    )
    replay = await billing_service(database, unavailable_fx).run(first.period_end)
    assert replay.errors == {} and replay.replayed == 0
    assert fx.calls == 1 and unavailable_fx.calls == 0
    assert (await servers.get(contract.server.id)).state is ServerLifecycleState.STOPPED


@pytest.mark.parametrize("conflict", ["wallet", "usd", "key", "end", "invalid_fx"])
async def test_renewal_binding_conflict_cannot_requote_or_debit(
    database: Database, conflict: str
) -> None:
    contract = await seed_contract(database)
    first = await capture_first_hour(database, contract)
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    renewal = period(contract, first.period_end, usd_minor=251 if conflict == "usd" else 250)
    if conflict == "wallet":
        other = await seed_contract(database, state=ServerLifecycleState.STOPPED)
        renewal = replace(renewal, wallet_id=other.wallet_id)
    bound = await repository.bind(renewal)
    if conflict in {"key", "end", "invalid_fx"}:
        if conflict == "key":
            changes = {"idempotency_key": f"corrupt:{uuid4()}"}
        elif conflict == "end":
            changes = {"period_end": to_db_utc(bound.period_end + timedelta(minutes=1))}
        else:
            snapshot = bound.fx_snapshot.to_dict()
            snapshot["rate"] = "2"
            changes = {"fx_snapshot": snapshot}
        async with database.factory.begin() as session:
            await session.execute(
                sa.update(PeriodRow).where(PeriodRow.id == bound.id).values(**changes)
            )
    before = await persisted(database, contract)
    unavailable = OfflineFx(unavailable=True)
    report = await billing_service(database, unavailable).run(first.period_end)
    assert contract.server.id in report.errors
    assert contract.server.id in report.stop_requested_server_ids
    assert report.charged == 0 and report.replayed == 0 and unavailable.calls == 0
    assert await persisted(database, contract) == before
    async with database.factory() as session:
        assert (
            await session.scalar(sa.select(PeriodRow.status).where(PeriodRow.id == bound.id))
            == "pending"
        )


@pytest.mark.parametrize(
    "conflict", ["amount", "currency", "entry_type", "reference", "description", "changed_fx"]
)
async def test_recovery_rejects_corrupt_receipt_without_advancing_or_charging_again(
    database: Database, conflict: str
) -> None:
    contract = await seed_contract(database)
    first = await capture_first_hour(database, contract)
    failed = await billing_service(
        database, OfflineFx(), InterruptCoverageWrite(database.factory)
    ).run(first.period_end)
    assert contract.server.id in failed.errors
    repository = SqlAlchemyPrepaidHourlyPeriodRepository(database.factory)
    renewal = await repository.get(contract.server.id, first.period_end)
    assert renewal is not None
    async with database.factory.begin() as session:
        if conflict == "changed_fx":
            replacement = quote(rate=120_000)
            await session.execute(
                sa.update(PeriodRow)
                .where(PeriodRow.id == renewal.id)
                .values(
                    irt_minor=replacement.target_amount_minor,
                    fx_snapshot=replacement.to_dict(),
                )
            )
        else:
            changes = {
                "amount": {"amount": renewal.irt_minor + 1},
                "currency": {"currency": "USD"},
                "entry_type": {"entry_type": "deposit"},
                "reference": {"reference_id": uuid4()},
                "description": {"description": "different financial fact"},
            }[conflict]
            await session.execute(
                sa.update(LedgerRow)
                .where(
                    LedgerRow.wallet_id == renewal.wallet_id,
                    LedgerRow.idempotency_key == renewal.idempotency_key,
                )
                .values(**changes)
            )
        await session.execute(
            sa.update(ServerRow).where(ServerRow.id == contract.server.id).values(state="stopped")
        )
    before = await persisted(database, contract)
    unavailable = OfflineFx(unavailable=True)
    rejected = await billing_service(database, unavailable).run(first.period_end)
    assert contract.server.id in rejected.errors
    assert rejected.charged == 0 and rejected.replayed == 0 and unavailable.calls == 0
    assert await persisted(database, contract) == before
    stored = await repository.get(contract.server.id, first.period_end)
    assert stored is not None and stored.status is PrepaidHourlyPeriodStatus.PENDING


async def test_deleted_while_fx_is_pending_cannot_debit_or_extend_coverage(
    database: Database,
) -> None:
    contract = await seed_contract(database)
    first = await capture_first_hour(database, contract)
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedFx(OfflineFx):
        async def snapshot(
            self, amount: int, source: str, target: str, purpose: FxPurpose
        ) -> ConversionSnapshot:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return await super().snapshot(amount, source, target, purpose)

    before = await persisted(database, contract)
    task = asyncio.create_task(billing_service(database, PausedFx()).run(first.period_end))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        servers = SqlAlchemyServerRepository(database.factory)
        deleted = await servers.get(contract.server.id)
        assert deleted is not None
        deleted.state = ServerLifecycleState.DELETED
        deleted.deleted_at = first.period_end
        await servers.save(deleted)
        release.set()
        report = await asyncio.wait_for(task, timeout=5)
        assert await persisted(database, contract) == before
        assert report.charged == 0 and report.replayed == 0
        stored = await SqlAlchemyPrepaidHourlyPeriodRepository(database.factory).get(
            contract.server.id, first.period_end
        )
        assert stored is not None and stored.status is PrepaidHourlyPeriodStatus.PENDING
        assert (await servers.get(contract.server.id)).state is ServerLifecycleState.DELETED
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_customer_resume_concurrently_buys_one_hour_without_charging_stopped_gap(database):
    contract = await seed_contract(database)
    first = await capture_first_hour(database, contract)
    servers = SqlAlchemyServerRepository(database.factory)
    row = await servers.get(contract.server.id)
    row.state = ServerLifecycleState.STOPPED
    await servers.save(row)
    before = await persisted(database, contract)
    services = [billing_service(database, OfflineFx()) for _ in range(2)]
    for service in services:
        service._lock = PostgresAdvisoryAccrualLock(database.factory)
    results = await asyncio.gather(
        *(service.ensure_customer_coverage(row) for service in services), return_exceptions=True
    )
    for service in services:
        await service.ensure_customer_coverage(row)
    after = await persisted(database, contract)
    assert any(not isinstance(result, Exception) for result in results)
    assert after[1] == before[1] - 250_000 and after[2] == before[2] + 1
    assert after[0] > to_db_utc(datetime.now(UTC))
    assert first.period_end < datetime.now(UTC)


async def test_real_runway_lock_and_log_deduplicate_competing_workers(database):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    contract = await seed_contract(database)
    now = datetime.now(UTC)
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(_event):
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=5)

    notifier = SimpleNamespace(send=AsyncMock(side_effect=send))
    log = SqlAlchemyLowBalanceNotificationLogRepository(database.factory)

    def worker():
        return HourlyRunwayWarningService(
            servers=SqlAlchemyServerRepository(database.factory),
            billing=billing_service(database, OfflineFx()),
            notifier=notifier,
            log=log,
            lock=PostgresAdvisoryAccrualLock(database.factory, key=777_072),
        )

    first = asyncio.create_task(worker().run(now))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert await worker().run(now) == 0
        release.set()
        assert await first == 1
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
    assert await log.last_runway_sent(contract.server.user_id) is not None
    assert await worker().run(now + timedelta(hours=5, minutes=59)) == 0
    assert notifier.send.await_count == 1
