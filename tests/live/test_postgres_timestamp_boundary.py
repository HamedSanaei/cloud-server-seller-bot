"""Opt-in live tests for the legacy naive-timestamp persistence contract.

These tests are skipped unless::

    CLOUD_PLATFORM_TEST_POSTGRES_URL=postgresql+asyncpg://... (a SCRATCH database)

They exist to prove what the mocked unit repositories cannot: asyncpg binds
the Python value it is handed, so writing an *aware* datetime into a legacy
``TIMESTAMP WITHOUT TIME ZONE`` column raises::

    asyncpg.exceptions.DataError: invalid input for query argument $3:
    datetime.datetime(... tzinfo=datetime.timezone.utc)
    (can't subtract offset-naive and offset-aware datetimes)

In production that aborted ``SqlAlchemyOperationRepository.claim`` *before*
the Leaseweb POST, so server ``7dbdf058`` stayed REQUESTED forever with
``operations.status = pending`` / ``attempts = 0``, and it killed the
Tetraminator ``payment_sessions.created_at <= <aware cutoff>`` scan.

The migration that runs here is the real one (alembic upgrade head), so the
column types under test are exactly production's.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cloud_platform.db.timestamps import from_db_utc, to_db_utc
from cloud_platform.modules.abuse.domain import AbuseCase, AbuseStatus, ResourceRef, ResourceType
from cloud_platform.modules.abuse.repository import SqlAlchemyAbuseCaseRepository
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
from cloud_platform.modules.operations.domain import OperationStatus, OperationType
from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
from cloud_platform.modules.orders.repository import SqlAlchemyProviderOrderRepository
from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.reconcile import reconcile_tetraminator_pending
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.users.domain import TermsVersion, User
from cloud_platform.modules.users.repository import (
    SqlAlchemyTermsVersionRepository,
    SqlAlchemyUserRepository,
)
from cloud_platform.modules.wallet.repository import SqlAlchemyHoldRepository

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()

requires_postgres = pytest.mark.skipif(
    not DB_URL,
    reason="live PostgreSQL tests require CLOUD_PLATFORM_TEST_POSTGRES_URL",
)

PROVIDER = "leaseweb"
RESOURCE_TYPE = "cloud_server"

#: Every table these tests write; truncated per test (CASCADE covers FKs).
_TOUCHED_TABLES = (
    "operations",
    "payment_sessions",
    "holds",
    "wallets",
    "ledger",
    "users",
    "terms_versions",
    "servers",
    "sellable_offers",
    "provider_orders",
    "abuse_cases",
    "catalog",
    "provider_accounts",
    "providers",
)


def _session_factory_for(url: str) -> Any:
    # NullPool: every checkout opens its own connection, so engines never pin
    # connections to a previous test's event loop.
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def factory_cm() -> Any:
        return factory()

    factory_cm.engine = engine  # type: ignore[attr-defined]
    return factory_cm


@pytest.fixture(scope="module")
def _migrated_db() -> Any:
    """Migrate the scratch database to head once per module (real migrations)."""
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["DATABASE_URL"] = DB_URL
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr[-2000:])
    yield None


@pytest.fixture()
async def _clean_db(_migrated_db: Any) -> AsyncIterator[Any]:
    """Fresh engine per test (own event loop) over the migrated schema."""
    _ = _migrated_db
    session_factory = _session_factory_for(DB_URL)
    engine = session_factory.engine
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TOUCHED_TABLES)} CASCADE"))
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _scalar(session_factory: Any, sql: str, **params: Any) -> Any:
    async with session_factory() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _seed_user(session_factory: Any, balance: int | None = None) -> Any:
    """One user row (plus a wallet when a balance is requested)."""
    from cloud_platform.db.base import User as _UserModel
    from cloud_platform.db.base import Wallet as _WalletModel

    user_id = uuid4()
    async with session_factory() as session:
        session.add(
            _UserModel(id=user_id, username=f"u-{user_id.hex[:8]}", email=f"{user_id.hex}@t.test")
        )
        if balance is not None:
            session.add(_WalletModel(id=uuid4(), user_id=user_id, balance=balance, currency="USD"))
        await session.commit()
    return user_id


async def _seed_server_bundle(session_factory: Any) -> tuple[Any, Any, Any]:
    """Minimal provider/account/catalog/server/offer rows: (server_id, offer_id, user_id)."""
    from cloud_platform.db.base import Catalog as _CatalogModel
    from cloud_platform.db.base import Provider as _ProviderModel
    from cloud_platform.db.base import ProviderAccount as _AccountModel
    from cloud_platform.db.base import SellableOffer as _OfferModel
    from cloud_platform.db.base import Server as _ServerModel
    from cloud_platform.db.base import User as _UserModel

    user_id, provider_id = uuid4(), uuid4()
    account_id, catalog_id, server_id, offer_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with session_factory() as session:
        session.add(
            _UserModel(id=user_id, username=f"u-{user_id.hex[:8]}", email=f"{user_id.hex}@t.test")
        )
        session.add(_ProviderModel(id=provider_id, name=PROVIDER))
        session.add(
            _AccountModel(id=account_id, provider_id=provider_id, user_id=user_id, status="active")
        )
        session.add(
            _CatalogModel(
                id=catalog_id,
                name="lsw.mini",
                provider_id=provider_id,
                provider_plan_id="lsw.mini",
                provider_location_id="eu-west-3",
                architecture="x86_64",
                vcpu=1,
                memory_mb=1024,
                disk_gb=25,
                price_per_quantum=2,
                currency="USD",
                quantum_seconds=3600,
            )
        )
        session.add(
            _OfferModel(
                id=offer_id,
                provider_key=PROVIDER,
                product_id="lsw.mini",
                location_id="eu-west-3",
                name="Mini",
                provider_cost_currency="EUR",
                selling_currency="USD",
            )
        )
        session.add(
            _ServerModel(
                id=server_id,
                user_id=user_id,
                provider_id=provider_id,
                provider_account_id=account_id,
                catalog_id=catalog_id,
                state="requested",
                price_per_quantum=6,
                currency="USD",
            )
        )
        await session.commit()
    return server_id, offer_id, user_id


@requires_postgres
class TestLiveOperationClaim:
    """The exact production failure: claiming an operation on real PostgreSQL."""

    async def _pending(self, session_factory: Any) -> Any:
        repo = SqlAlchemyOperationRepository(session_factory)
        return await repo.get_or_create(
            operation_key=f"server-create:{uuid4()}",
            operation_type=OperationType.SERVER_CREATE,
            resource_type=RESOURCE_TYPE,
            resource_id=uuid4(),
            provider_key=PROVIDER,
        )

    async def test_aware_bind_is_rejected_by_the_naive_column(self, _clean_db: Any) -> None:
        """Pins WHY the contract exists (this is the production stack trace)."""
        from sqlalchemy.exc import DBAPIError

        operation = await self._pending(_clean_db)
        async with _clean_db() as session:
            with pytest.raises(DBAPIError) as caught:
                await session.execute(
                    text("update operations set updated_at = :ts where id = :id"),
                    {"ts": datetime.now(UTC), "id": operation.id},
                )
        assert "asyncpg.exceptions.DataError" in str(caught.value.orig)
        assert "offset-naive and offset-aware datetimes" in str(caught.value)
        # The rejected bind left the row PENDING, exactly like production.
        status = await _scalar(
            _clean_db, "select status from operations where id = :id", id=operation.id
        )
        assert status == "pending"

    async def test_claim_moves_pending_to_in_flight(self, _clean_db: Any) -> None:
        repo = SqlAlchemyOperationRepository(_clean_db)
        operation = await self._pending(_clean_db)
        assert operation.status is OperationStatus.PENDING
        assert operation.attempts == 0

        claimed = await repo.claim(operation.id)

        assert claimed is not None
        assert claimed.status is OperationStatus.IN_FLIGHT
        assert claimed.attempts == 1
        assert claimed.updated_at is not None and claimed.updated_at.tzinfo is not None
        # Stored exactly as naive UTC, and rehydrated as aware UTC.
        stored = await _scalar(
            _clean_db, "select updated_at from operations where id = :id", id=operation.id
        )
        assert stored.tzinfo is None
        assert abs((from_db_utc(stored) - claimed.updated_at).total_seconds()) < 1

    async def test_second_claim_loses_the_race(self, _clean_db: Any) -> None:
        repo = SqlAlchemyOperationRepository(_clean_db)
        operation = await self._pending(_clean_db)
        assert await repo.claim(operation.id) is not None
        assert await repo.claim(operation.id) is None

    async def test_requeue_then_reclaim_keeps_the_same_key(self, _clean_db: Any) -> None:
        repo = SqlAlchemyOperationRepository(_clean_db)
        operation = await self._pending(_clean_db)
        claimed = await repo.claim(operation.id)
        assert claimed is not None
        claimed_at = claimed.updated_at

        claimed.requeue("transient provider failure")
        requeued = await repo.save(claimed)

        assert requeued.status is OperationStatus.PENDING
        assert requeued.error == "transient provider failure"
        assert requeued.operation_key == operation.operation_key
        assert requeued.updated_at is not None
        assert claimed_at is not None and requeued.updated_at >= claimed_at

        reclaimed = await repo.claim(operation.id)
        assert reclaimed is not None
        assert reclaimed.status is OperationStatus.IN_FLIGHT
        assert reclaimed.attempts == 2

    async def test_terminal_failure_is_persisted_and_listed(self, _clean_db: Any) -> None:
        repo = SqlAlchemyOperationRepository(_clean_db)
        operation = await self._pending(_clean_db)
        claimed = await repo.claim(operation.id)
        assert claimed is not None
        claimed.fail("no price snapshot")

        saved = await repo.save(claimed)

        assert saved.status is OperationStatus.FAILED
        failed = await repo.list_failed(operation_types=[OperationType.SERVER_CREATE])
        assert [row.id for row in failed] == [operation.id]
        assert failed[0].updated_at is not None and failed[0].updated_at.tzinfo is not None
        assert await repo.claim(operation.id) is None

    async def test_get_or_create_keeps_one_row_per_key(self, _clean_db: Any) -> None:
        repo = SqlAlchemyOperationRepository(_clean_db)
        operation = await self._pending(_clean_db)
        again = await repo.get_or_create(
            operation_key=operation.operation_key,
            operation_type=OperationType.SERVER_CREATE,
            resource_type=RESOURCE_TYPE,
            resource_id=operation.resource_id,
            provider_key=PROVIDER,
        )
        assert again.id == operation.id
        count = await _scalar(
            _clean_db,
            "select count(*) from operations where operation_key = :key",
            key=operation.operation_key,
        )
        assert count == 1

    async def test_oldest_pending_age_accepts_an_aware_now(self, _clean_db: Any) -> None:
        repo = SqlAlchemyOperationRepository(_clean_db)
        types = [OperationType.SERVER_CREATE]
        assert await repo.oldest_pending_age_seconds(types, datetime.now(UTC)) is None
        await self._pending(_clean_db)
        age = await repo.oldest_pending_age_seconds(types, datetime.now(UTC))
        assert age is not None and age >= 0


@requires_postgres
class TestLivePaymentSessionReconciliation:
    """``payment_sessions.created_at <= <aware cutoff>`` must bind."""

    async def _create(self, session_factory: Any, *, gateway: str = "tetraminator") -> Any:
        repo = SqlAlchemyPaymentSessionRepository(session_factory)
        return await repo.create(
            PaymentSession(
                user_id=uuid4(),
                gateway_key=gateway,
                gateway_payment_id=f"pay-{uuid4().hex[:8]}",
                amount_minor=50_000,
                currency="IRT",
                idempotency_key=f"recharge-{uuid4()}",
            )
        )

    async def test_scan_with_an_aware_cutoff_finds_old_sessions(self, _clean_db: Any) -> None:
        repo = SqlAlchemyPaymentSessionRepository(_clean_db)
        session = await self._create(_clean_db)
        older = datetime.now(UTC) - timedelta(minutes=40)
        async with _clean_db() as db:
            await db.execute(
                text("update payment_sessions set created_at = :ts where id = :id"),
                {"ts": to_db_utc(older), "id": session.id},
            )
            await db.commit()

        # Fresh session: younger than the grace window, so not a candidate.
        fresh = await self._create(_clean_db)
        cutoff = datetime.now(UTC) - timedelta(minutes=10)

        pending = await repo.list_pending_before("tetraminator", cutoff)

        assert [row.id for row in pending] == [session.id]
        assert fresh.id not in [row.id for row in pending]
        assert pending[0].created_at is not None and pending[0].created_at.tzinfo is not None

    async def test_reconcile_scan_reaches_sessions(self, _clean_db: Any) -> None:
        repo = SqlAlchemyPaymentSessionRepository(_clean_db)
        session = await self._create(_clean_db)
        async with _clean_db() as db:
            await db.execute(
                text("update payment_sessions set created_at = :ts where id = :id"),
                {"ts": to_db_utc(datetime.now(UTC) - timedelta(minutes=40)), "id": session.id},
            )
            await db.commit()

        class _PendingIntent:
            status = PaymentSessionStatus.PENDING

        class _Gateway:
            async def verify_payment(self, external_id: str) -> Any:
                return _PendingIntent()

        class _Webhook:
            async def process_callback(self, **kwargs: Any) -> Any:  # pragma: no cover
                raise AssertionError("a still-pending inquiry must never settle")

        report = await reconcile_tetraminator_pending(
            payments_repo=repo,
            webhook_service=_Webhook(),
            gateway=_Gateway(),
            stale_after=timedelta(minutes=10),
            now=datetime.now(UTC),
        )

        # The scan itself succeeded and inspected exactly the stale session.
        assert report.errors == 0
        assert report.checked == 1
        assert report.still_pending == 1

    async def test_credit_persists_naive_utc(self, _clean_db: Any) -> None:
        repo = SqlAlchemyPaymentSessionRepository(_clean_db)
        session = await self._create(_clean_db)
        succeeded = session.mark_succeeded(gateway_payment_id=session.gateway_payment_id or "")
        succeeded = await repo.save(succeeded)
        credited = succeeded.mark_credited(at=datetime.now(UTC))

        saved = await repo.save(credited)

        assert saved.credited_at is not None and saved.credited_at.tzinfo is not None
        stored = await _scalar(
            _clean_db, "select credited_at from payment_sessions where id = :id", id=session.id
        )
        assert stored.tzinfo is None
        updated = await _scalar(
            _clean_db, "select updated_at from payment_sessions where id = :id", id=session.id
        )
        created = await _scalar(
            _clean_db, "select created_at from payment_sessions where id = :id", id=session.id
        )
        assert updated is not None and created is not None and updated >= created
        # fx_observed_at is a real timestamptz column: it must stay aware.
        fx = await _scalar(
            _clean_db, "select fx_observed_at from payment_sessions where id = :id", id=session.id
        )
        assert fx is None


@requires_postgres
class TestLiveLegacyNaiveWriters:
    """Every other repository that binds a domain datetime to a naive column."""

    async def test_hold_capture_and_release(self, _clean_db: Any) -> None:
        from cloud_platform.modules.wallet.domain import DEFAULT_WALLET_CURRENCY

        user_id = await _seed_user(_clean_db, balance=100_000)
        wallet_id = await _scalar(
            _clean_db, "select id from wallets where user_id = :uid", uid=user_id
        )
        assert wallet_id is not None
        holders = SqlAlchemyHoldRepository(_clean_db)

        held = await holders.create_hold(wallet_id, 25_000, DEFAULT_WALLET_CURRENCY, "hold-a")
        captured = await holders.capture_hold(held.id)
        assert captured is not None and captured.captured_at is not None
        stored = await _scalar(
            _clean_db, "select captured_at from holds where id = :id", id=held.id
        )
        assert stored.tzinfo is None

        second = await holders.create_hold(wallet_id, 10_000, DEFAULT_WALLET_CURRENCY, "hold-b")
        released = await holders.release_hold(second.id)
        assert released is not None and released.released_at is not None
        assert released.released_at.tzinfo is not None
        stored_release = await _scalar(
            _clean_db, "select released_at from holds where id = :id", id=second.id
        )
        assert stored_release.tzinfo is None

    async def test_user_terms_acceptance_and_terms_publish(self, _clean_db: Any) -> None:
        users_repo = SqlAlchemyUserRepository(_clean_db)
        created = await users_repo.create(
            User(username=f"u-{uuid4().hex[:8]}", email=f"{uuid4().hex}@t.test")
        )
        assert created.id is not None

        accepted = await users_repo.update_terms(created.id, 1)

        assert accepted.terms_accepted_at is not None
        assert accepted.terms_accepted_at.tzinfo is not None
        stored = await _scalar(
            _clean_db, "select terms_accepted_at from users where id = :id", id=created.id
        )
        assert stored.tzinfo is None

        terms_repo = SqlAlchemyTermsVersionRepository(_clean_db)
        published = await terms_repo.publish(
            TermsVersion(version=1, body="Terms body", effective_at=datetime.now(UTC))
        )
        assert published.effective_at.tzinfo is not None
        stored_effective = await _scalar(
            _clean_db, "select effective_at from terms_versions where version = 1"
        )
        assert stored_effective.tzinfo is None

    async def test_server_deleted_at_is_normalized(self, _clean_db: Any) -> None:
        server_id, _offer_id, _user_id = await _seed_server_bundle(_clean_db)
        repo = SqlAlchemyServerRepository(_clean_db)
        server = await repo.get(server_id)
        assert server is not None

        server.deleted_at = datetime.now(UTC)
        saved = await repo.save(server)

        assert saved.deleted_at is not None and saved.deleted_at.tzinfo is not None
        stored = await _scalar(
            _clean_db, "select deleted_at from servers where id = :id", id=server_id
        )
        assert stored.tzinfo is None

    async def test_provider_order_save_writes_a_naive_utc_timestamp(self, _clean_db: Any) -> None:
        server_id, offer_id, _user_id = await _seed_server_bundle(_clean_db)
        order_id = uuid4()
        async with _clean_db() as db:
            await db.execute(
                text(
                    "insert into provider_orders "
                    "(id, server_id, operation_key, provider_key, offer_id, status) "
                    "values (:id, :server_id, :key, :provider, :offer_id, 'submitted')"
                ),
                {
                    "id": order_id,
                    "server_id": server_id,
                    "key": f"order-create:{server_id}",
                    "provider": PROVIDER,
                    "offer_id": offer_id,
                },
            )
            await db.commit()

        repo = SqlAlchemyProviderOrderRepository(_clean_db)
        order = await repo.get(order_id)
        assert order is not None
        before = await _scalar(
            _clean_db, "select updated_at from provider_orders where id = :id", id=order_id
        )

        saved = await repo.save(order)

        assert saved.updated_at is not None and saved.updated_at.tzinfo is not None
        stored = await _scalar(
            _clean_db, "select updated_at from provider_orders where id = :id", id=order_id
        )
        assert stored.tzinfo is None
        assert before is None or stored >= before

    async def test_abuse_case_transition_persists_naive_utc(self, _clean_db: Any) -> None:
        server_id, _offer_id, user_id = await _seed_server_bundle(_clean_db)
        repo = SqlAlchemyAbuseCaseRepository(_clean_db)
        created = await repo.create(
            AbuseCase(
                resource=ResourceRef(
                    provider_key=PROVIDER,
                    resource_type=ResourceType.PROVIDER_SERVER,
                    resource_id=str(server_id),
                ),
                user_id=user_id,
                server_id=server_id,
                reason="test finding",
                reporter="unit",
            )
        )
        assert created.id is not None

        moved = created.transition(AbuseStatus.INVESTIGATING, at=datetime.now(UTC))
        saved = await repo.save(moved)

        assert saved.updated_at is not None and saved.updated_at.tzinfo is not None
        stored = await _scalar(
            _clean_db, "select updated_at from abuse_cases where id = :id", id=created.id
        )
        assert stored.tzinfo is None
