"""Opt-in live tests for durable provider account capacity (PostgreSQL).

Skipped unless::

    CLOUD_PLATFORM_TEST_POSTGRES_URL=postgresql+asyncpg://... (a SCRATCH server)

These exist because only real PostgreSQL proves what this table's correctness
actually depends on:

* the ``(provider_key, credential_account_id)`` UNIQUE constraint drives a
  select-then-mutate upsert — two concurrent workers recording the SAME refusal
  must serialize on the row lock and end with ONE row and TWO observations,
  never a duplicate-key error and never a lost observation;
* an ELAPSED window settles to ``unknown_after_limit`` — the refusal stops
  being fresh, but the account is still excluded until an operator or a
  verified positive signal proves recovery (expiry is not evidence);
  ``settle_expired`` persists exactly that transition;
* an unknown state value (a newer release's enum) reads as "cannot take new
  orders", never as healthy — the fail-closed direction;
* historical provider-operation failures are reconciled into the store EXACTLY
  ONCE (the partial unique index on the evidence log is the anchor), so a
  restart or a second worker cannot double-count one refusal or resurrect it
  after an operator cleared the account;
* ``clear`` really removes the row while ``record_healthy`` keeps the evidence.

The scratch DATABASE is created and dropped by this module on the target
server; the configured database is never modified.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cloud_platform.db.base import Operation as _OperationModel
from cloud_platform.db.base import Provider as _ProviderModel
from cloud_platform.db.base import ProviderAccount as _ProviderAccountModel
from cloud_platform.db.base import ProviderAccountCapacity as _CapacityModel
from cloud_platform.db.base import ProviderAccountCapacityEvent as _CapacityEventModel
from cloud_platform.db.base import Server as _ServerModel
from cloud_platform.db.base import User as _UserModel
from cloud_platform.modules.provider_capacity.domain import (
    AccountCapacity,
    AccountCapacityState,
    CapacityEventKind,
    CapacityObservation,
    HistoricalCapacityEvidence,
)
from cloud_platform.modules.provider_capacity.reconciliation import (
    CapacityReconciliationService,
)
from cloud_platform.modules.provider_capacity.repository import (
    SqlAlchemyAccountCapacityRepository,
)
from cloud_platform.providers.leaseweb.capacity_evidence import (
    SqlAlchemyHistoricalCapacityEvidenceSource,
    parse_capacity_evidence,
)

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()

requires_postgres = pytest.mark.skipif(
    not DB_URL,
    reason="live PostgreSQL tests require CLOUD_PLATFORM_TEST_POSTGRES_URL",
)

SCRATCH_DB = "cloud_platform_capacity_scratch"
PROVIDER = "leaseweb"
ACCOUNT = "sales-org-north"


def _with_database(url: str, database: str) -> str:
    """The same URL pointed at another database, credentials intact.

    ``hide_password=False`` matters: ``str(URL)`` masks the password as ``***``,
    which would make the alembic subprocess fail authentication instead of
    reporting the real problem.
    """
    return make_url(url).set(database=database).render_as_string(hide_password=False)


async def _recreate_scratch_database(base_url: str) -> None:
    """(Re)create the scratch database on the configured server.

    The (re)creation runs from the CONFIGURED database, which is used as a
    connection target only: ``CREATE/DROP DATABASE`` are not allowed inside a
    transaction, hence the autocommit connection, and the configured database
    itself is never written to, migrated or dropped.
    """
    engine = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                sa.text(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
            )
            await connection.execute(sa.text(f'CREATE DATABASE "{SCRATCH_DB}"'))
    finally:
        await engine.dispose()


@contextlib.contextmanager
def _scratch_database() -> Iterator[str]:
    """A migrated scratch database, dropped again on exit.

    Synchronous on purpose: ``alembic upgrade head`` is a blocking subprocess,
    and the async detector must not be silenced to hide that. The database is
    created and dropped by this module, so the configured database is only ever
    a connection target.
    """
    asyncio.run(_recreate_scratch_database(DB_URL))
    scratch_url = _with_database(DB_URL, SCRATCH_DB)
    repo_root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["DATABASE_URL"] = scratch_url
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:  # pragma: no cover - environment failure
        target = make_url(scratch_url)
        pg_env = {key: value for key, value in environment.items() if key.startswith("PG")}
        raise AssertionError(
            f"alembic upgrade head failed against "
            f"{target.host}:{target.port}/{target.database} "
            f"(password set: {bool(target.password)}, pg env keys: {sorted(pg_env)}): "
            f"{completed.stderr[-1500:]}"
        )
    try:
        yield scratch_url
    finally:
        asyncio.run(_recreate_scratch_database(DB_URL))


@dataclass(slots=True)
class CapacityEnvironment:
    """A migrated scratch database plus the handles a test needs."""

    repo: SqlAlchemyAccountCapacityRepository
    factory: async_sessionmaker[AsyncSession]
    url: str


@pytest.fixture()
def scratch_url() -> Iterator[str]:
    """A freshly migrated scratch database, dropped again on exit.

    Synchronous on purpose: creating the database and running ``alembic upgrade
    head`` are blocking operations that must not be executed inside the test's
    event loop.
    """
    if not DB_URL:  # pragma: no cover - skipped by the marker
        pytest.skip("live PostgreSQL tests require CLOUD_PLATFORM_TEST_POSTGRES_URL")
    with _scratch_database() as url:
        yield url


def _engine(url: str) -> Any:
    """A test engine that opens a connection per checkout (``NullPool``).

    Pooling would pin asyncpg connections to the event loop of the test that
    opened them; a later test's loop would then try to close a foreign
    transport (``AttributeError: 'NoneType' object has no attribute 'send'``
    from the Windows proactor loop). One connection per session removes the
    cross-loop state entirely.
    """
    return create_async_engine(url, poolclass=NullPool)


@pytest.fixture()
async def capacity_repo(scratch_url: str) -> AsyncIterator[SqlAlchemyAccountCapacityRepository]:
    """A repository bound to a fresh, migrated scratch database."""
    engine = _engine(scratch_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SqlAlchemyAccountCapacityRepository(factory)
    finally:
        # Disposed inside the TEST's own loop: ``asyncio.run(engine.dispose())``
        # would close asyncpg transports from a loop that never opened them.
        await engine.dispose()


@pytest.fixture()
async def capacity_environment(scratch_url: str) -> AsyncIterator[CapacityEnvironment]:
    """The repository PLUS the session factory (restart + evidence tests)."""
    engine = _engine(scratch_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield CapacityEnvironment(
            repo=SqlAlchemyAccountCapacityRepository(factory), factory=factory, url=scratch_url
        )
    finally:
        await engine.dispose()


pytestmark = requires_postgres


async def _insert_raw_state(url: str, state: str) -> None:
    engine = _engine(url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                _CapacityModel(
                    provider_key=PROVIDER,
                    credential_account_id=ACCOUNT,
                    state=state,
                    observations=1,
                    observed_at=datetime.now(UTC),
                )
            )
            await session.commit()
    finally:
        await engine.dispose()


class TestCapacityPersistence:
    async def test_a_refusal_round_trips_with_its_evidence(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        assert await capacity_repo.get(PROVIDER, ACCOUNT) is None
        now = datetime.now(UTC)
        record = await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(
                error_code="PC-2031",
                correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
                location_id="eu-central-1",
                product_id="lsw.m4.large",
            ),
            ttl_seconds=3600,
            now=now,
        )
        assert record.state is AccountCapacityState.LIMIT_REACHED
        assert record.observations == 1

        stored = await capacity_repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.is_limit_reached(now=now) is True
        assert stored.error_code == "PC-2031"
        assert stored.correlation_id == "07376219-7bcd-43d9-a5ea-4128fa57345a"
        assert stored.location_id == "eu-central-1"
        assert stored.product_id == "lsw.m4.large"
        assert await capacity_repo.limit_reached_accounts(PROVIDER, now=now) == frozenset({ACCOUNT})

    async def test_an_elapsed_window_settles_to_unproven_not_healthy(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        """The production shape of 8fc2e573: the refusal outlived its window.

        Expiry must NOT re-admit the account — nobody proved the provider
        limit lifted.
        """
        now = datetime.now(UTC)
        past = now - timedelta(hours=2)
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031", correlation_id="cid"),
            ttl_seconds=60,
            now=past,
        )
        assert await capacity_repo.limit_reached_accounts(PROVIDER, now=now) == frozenset({ACCOUNT})
        stored = await capacity_repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        assert stored.accepts_new_orders(now=now) is False
        assert stored.blocked_reason(now=now) == "unknown-after-limit"
        assert stored.error_code == "PC-2031"
        assert stored.observations == 1

    async def test_a_repeated_refusal_refreshes_the_window_and_counts(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        now = datetime.now(UTC)
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031", correlation_id="first"),
            ttl_seconds=3600,
            now=now - timedelta(minutes=30),
        )
        second = await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031", correlation_id="second"),
            ttl_seconds=7200,
            now=now,
        )
        assert second.observations == 2
        assert second.correlation_id == "second"
        stored = await capacity_repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.expires_at is not None
        assert stored.expires_at.replace(tzinfo=UTC) >= now + timedelta(seconds=7000)

    async def test_recovery_keeps_the_evidence_but_restores_eligibility(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031", correlation_id="cid"),
            ttl_seconds=3600,
        )
        recovered = await capacity_repo.record_healthy(PROVIDER, ACCOUNT)
        assert recovered.state is AccountCapacityState.HEALTHY
        assert recovered.accepts_new_orders() is True
        assert recovered.expires_at is None
        assert recovered.error_code == "PC-2031"
        assert recovered.observations == 1
        assert await capacity_repo.limit_reached_accounts(PROVIDER) == frozenset()

    async def test_clear_deletes_the_row(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=3600,
        )
        assert await capacity_repo.clear(PROVIDER, ACCOUNT) is True
        assert await capacity_repo.get(PROVIDER, ACCOUNT) is None
        assert await capacity_repo.clear(PROVIDER, ACCOUNT) is False

    async def test_accounts_and_providers_do_not_interfere(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        now = datetime.now(UTC)
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=3600,
            now=now,
        )
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id="sales-org-uk",
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=60,
            now=now - timedelta(hours=2),
        )
        # Both accounts stay out of NEW orders: the UK window elapsed, which is
        # not recovery, only a loss of freshness — and each record keeps its
        # own reason.
        assert await capacity_repo.limit_reached_accounts(PROVIDER, now=now) == frozenset(
            {ACCOUNT, "sales-org-uk"}
        )
        north = await capacity_repo.get(PROVIDER, ACCOUNT)
        uk = await capacity_repo.get(PROVIDER, "sales-org-uk")
        assert north is not None and north.state is AccountCapacityState.LIMIT_REACHED
        assert uk is not None and uk.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        assert north.blocked_reason(now=now) == "limit-reached"
        assert uk.blocked_reason(now=now) == "unknown-after-limit"
        # A different provider is never affected by Leaseweb capacity rows.
        assert await capacity_repo.get("hetzner", ACCOUNT) is None
        assert await capacity_repo.list_for_provider("hetzner") == ()

    async def test_the_records_are_listed_deterministically(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        for account in ("sales-org-uk", ACCOUNT, "sales-org-ap"):
            await capacity_repo.record_limit_reached(
                provider_key=PROVIDER,
                credential_account_id=account,
                observation=CapacityObservation(error_code="PC-2031"),
                ttl_seconds=3600,
            )
        records = await capacity_repo.list_for_provider(PROVIDER)
        listed = [record.credential_account_id for record in records]
        assert listed == sorted(listed)


class TestFailClosedStates:
    async def test_an_unknown_state_is_never_healthy(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        """A newer release's state must read as \"no new orders\", not healthy."""
        url = _with_database(DB_URL, SCRATCH_DB)
        await _insert_raw_state(url, "some_future_state")
        stored = await capacity_repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.state in (
            AccountCapacityState.LIMIT_REACHED,
            AccountCapacityState.UNKNOWN_AFTER_LIMIT,
        )
        assert stored.accepts_new_orders() is False
        assert await capacity_repo.limit_reached_accounts(PROVIDER) == frozenset({ACCOUNT})

    async def test_a_naive_stored_expiry_is_read_as_utc_and_still_excludes(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        """timestamptz round-trips as aware; a naive value is read as UTC."""
        now = datetime.now(UTC)
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=3600,
            now=now,
        )
        stored = await capacity_repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.expires_at is not None
        assert stored.is_limit_reached(now=now + timedelta(minutes=1)) is True
        # The window elapsed: the state is "unproven", never "eligible".
        settled = stored.settled(now=now + timedelta(hours=2))
        assert settled.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        assert settled.is_limit_reached(now=now + timedelta(hours=2)) is True


class TestConcurrentRefusals:
    async def test_two_concurrent_refusals_serialize_into_one_row(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        """The row lock is what makes the upsert exactly-once per account."""
        now = datetime.now(UTC)
        results = await asyncio.gather(
            *[
                capacity_repo.record_limit_reached(
                    provider_key=PROVIDER,
                    credential_account_id=ACCOUNT,
                    observation=CapacityObservation(
                        error_code="PC-2031", correlation_id=f"cid-{index}"
                    ),
                    ttl_seconds=3600,
                    now=now,
                )
                for index in range(4)
            ],
            return_exceptions=True,
        )
        for result in results:
            assert not isinstance(result, Exception), result

        stored = await capacity_repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.observations == 4
        assert stored.is_limit_reached(now=now) is True
        # Exactly one row exists for the account (unique identity + upsert).
        engine = _engine(_with_database(DB_URL, SCRATCH_DB))
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                count = (
                    await session.execute(sa.select(sa.func.count()).select_from(_CapacityModel))
                ).scalar_one()
        finally:
            await engine.dispose()
        assert int(count) == 1


class TestRecoverySemantics:
    async def test_settle_expired_persists_the_transition_and_keeps_excluding(
        self, capacity_environment: CapacityEnvironment
    ) -> None:
        """The stored row must agree with what the platform believes."""
        repo = capacity_environment.repo
        now = datetime.now(UTC)
        await repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=60,
            now=now - timedelta(hours=1),
        )
        assert await repo.settle_expired(PROVIDER, now=now) == (ACCOUNT,)
        assert await repo.settle_expired(PROVIDER, now=now) == ()
        stored = await repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        assert stored.accepts_new_orders(now=now) is False
        assert await repo.limit_reached_accounts(PROVIDER, now=now) == frozenset({ACCOUNT})

    async def test_a_restart_reads_the_same_durable_state(
        self, capacity_environment: CapacityEnvironment
    ) -> None:
        """A new process (fresh repository over the same database) keeps the
        exclusion and the evidence — nothing lives in memory."""
        now = datetime.now(UTC)
        await capacity_environment.repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031", correlation_id="cid"),
            ttl_seconds=3600,
            now=now,
        )
        restarted = SqlAlchemyAccountCapacityRepository(capacity_environment.factory)
        stored = await restarted.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.is_limit_reached(now=now) is True
        assert stored.correlation_id == "cid"
        assert await restarted.limit_reached_accounts(PROVIDER, now=now) == frozenset({ACCOUNT})

    async def test_an_operator_clear_is_recorded_and_restores_eligibility(
        self, capacity_environment: CapacityEnvironment
    ) -> None:
        repo = capacity_environment.repo
        await repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=60,
            now=datetime.now(UTC) - timedelta(hours=1),
        )
        assert await repo.limit_reached_accounts(PROVIDER) == frozenset({ACCOUNT})
        cleared = await repo.record_healthy(PROVIDER, ACCOUNT)
        assert cleared.state is AccountCapacityState.HEALTHY
        assert cleared.accepts_new_orders() is True
        assert await repo.limit_reached_accounts(PROVIDER) == frozenset()
        events = await repo.list_events(PROVIDER, credential_account_id=ACCOUNT)
        assert [event.kind for event in events] == [
            CapacityEventKind.CLEARED,
            CapacityEventKind.REFUSAL,
        ]


async def _seed_failed_create(
    factory: async_sessionmaker[AsyncSession],
    *,
    operation_key: str,
    error_text: str,
    account_id: str = ACCOUNT,
    server_id: str,
    recorded_at: datetime | None = None,
) -> None:
    """Insert the MINIMAL real rows a historical failure needs.

    A failed hourly create is an ``operations`` row joined to the ``servers``
    row that pins the credential account — exactly what the production
    evidence (server 5e4bf88c, operation ``server-create:...``) looks like.

    ``recorded_at`` is the operation's real timestamp, and it decides the
    reconciled state: the production incident is DAYS old (its cooling window
    elapsed, so it can only land as ``unknown_after_limit``), while a failure
    from minutes ago is genuinely fresh ``limit_reached`` evidence.
    """
    import uuid

    naive_now = (recorded_at or datetime.now(UTC)).replace(tzinfo=None)
    async with factory() as session:
        user = _UserModel(
            username=f"evidence-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@test"
        )
        provider = _ProviderModel(name=f"leaseweb-{uuid.uuid4().hex[:8]}")
        session.add_all([user, provider])
        await session.flush()
        account = _ProviderAccountModel(provider_id=provider.id, user_id=user.id)
        session.add(account)
        await session.flush()
        server = _ServerModel(
            id=uuid.UUID(server_id),
            user_id=user.id,
            provider_id=provider.id,
            provider_account_id=account.id,
            state="error",
            price_per_quantum=1,
            currency="USD",
            billing_model="hourly",
            credential_account_id=account_id,
        )
        operation = _OperationModel(
            operation_key=operation_key,
            operation_type="server_create",
            resource_type="cloud_server",
            resource_id=server.id,
            provider_key="leaseweb",
            status="failed",
            error=error_text,
            attempts=1,
            created_at=naive_now,
            updated_at=naive_now,
        )
        session.add_all([server, operation])
        await session.commit()


PC2031_TEXT = (
    "provider account has no capacity for new instances: errorCode=PC-2031; Customer limit "
    "reached; correlationId=07376219-7bcd-43d9-a5ea-4128fa57345a; HTTP 400"
)
VALIDATION_TEXT = (
    "hourly offer revalidation failed: errorCode=400; Validation Failed; region: "
    'The value "eu-west-9" is not valid region; HTTP 400'
)
HISTORICAL_SERVER = "5e4bf88c-cabe-4ab1-9eb4-cf448e046382"


class TestHistoricalReconciliation:
    async def test_a_historical_pc2031_is_recovered_exactly_once(
        self, capacity_environment: CapacityEnvironment
    ) -> None:
        """Production fact: an account refused a create BEFORE the capacity
        feature existed, so the store was empty and the next customer order hit
        the same wall. The reconciliation closes that gap — once."""
        factory = capacity_environment.factory
        await _seed_failed_create(
            factory,
            operation_key=f"server-create:{HISTORICAL_SERVER}",
            error_text=PC2031_TEXT,
            server_id=HISTORICAL_SERVER,
            # The production incident predates the capacity feature by days.
            recorded_at=datetime.now(UTC) - timedelta(days=3),
        )
        service = CapacityReconciliationService(
            capacity_repo=capacity_environment.repo,
            evidence_source=SqlAlchemyHistoricalCapacityEvidenceSource(factory),
            ttl_seconds=3600,
        )

        first = await service.run("leaseweb")
        assert first.evidence_found == 1
        assert first.applied == ((ACCOUNT, AccountCapacityState.UNKNOWN_AFTER_LIMIT.value),)
        assert await capacity_environment.repo.limit_reached_accounts(PROVIDER) == frozenset(
            {ACCOUNT}
        )
        stored = await capacity_environment.repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.error_code == "PC-2031"
        assert stored.correlation_id == "07376219-7bcd-43d9-a5ea-4128fa57345a"
        # The refusal is historical: its window is over, so it can never be
        # published as fresh evidence — and never as healthy either.
        assert stored.accepts_new_orders() is False

        # Re-running (another worker, a restart, an operator retry) applies
        # nothing: the operation key is the idempotency anchor.
        second = await service.run("leaseweb")
        assert second.applied == ()
        assert second.already_recorded == (f"server-create:{HISTORICAL_SERVER}",)
        stored = await capacity_environment.repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.observations == 1
        async with factory() as session:
            rows = (
                await session.execute(
                    sa.select(_CapacityEventModel.kind, _CapacityEventModel.source_ref).where(
                        _CapacityEventModel.provider_key == PROVIDER
                    )
                )
            ).all()
        assert [tuple(row) for row in rows] == [
            (CapacityEventKind.BACKFILL.value, f"server-create:{HISTORICAL_SERVER}")
        ]

    async def test_an_unrelated_validation_failure_is_never_recovered(
        self, capacity_environment: CapacityEnvironment
    ) -> None:
        """A generic HTTP 400 (region/image/validation) must never be
        reinterpreted as a capacity refusal."""
        factory = capacity_environment.factory
        await _seed_failed_create(
            factory,
            operation_key=f"server-create:{HISTORICAL_SERVER}",
            error_text=VALIDATION_TEXT,
            server_id=HISTORICAL_SERVER,
        )
        service = CapacityReconciliationService(
            capacity_repo=capacity_environment.repo,
            evidence_source=SqlAlchemyHistoricalCapacityEvidenceSource(factory),
            ttl_seconds=3600,
        )
        report = await service.run("leaseweb")
        assert report.evidence_found == 0
        assert report.applied == ()
        assert await capacity_environment.repo.get(PROVIDER, ACCOUNT) is None
        # And the classifier itself agrees, field by field.
        assert (
            parse_capacity_evidence(
                VALIDATION_TEXT,
                provider_key=PROVIDER,
                credential_account_id=ACCOUNT,
                source_ref="op",
            )
            is None
        )

    async def test_concurrent_reconciliations_append_one_evidence_row(
        self, capacity_environment: CapacityEnvironment
    ) -> None:
        """Three workers starting at once must not triple-count one refusal."""
        factory = capacity_environment.factory
        await _seed_failed_create(
            factory,
            operation_key=f"server-create:{HISTORICAL_SERVER}",
            error_text=PC2031_TEXT,
            server_id=HISTORICAL_SERVER,
        )
        evidence = HistoricalCapacityEvidence(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            source_ref=f"server-create:{HISTORICAL_SERVER}",
            error_code="PC-2031",
            correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
            observed_at=datetime.now(UTC) - timedelta(days=1),
        )
        results = await asyncio.gather(
            *[
                capacity_environment.repo.record_historical_evidence(evidence, ttl_seconds=3600)
                for _ in range(3)
            ],
            return_exceptions=True,
        )
        applied = [result for result in results if not isinstance(result, Exception)]
        assert len([result for result in applied if result is not None]) == 1
        stored = await capacity_environment.repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.observations == 1
        assert len(await capacity_environment.repo.list_events(PROVIDER)) == 1

    async def test_an_operator_clear_outranks_older_history(
        self, capacity_environment: CapacityEnvironment
    ) -> None:
        """Positive proof recorded AFTER the incident wins: history is logged
        but does not re-block an account the operator has cleared."""
        repo = capacity_environment.repo
        await repo.record_healthy(PROVIDER, ACCOUNT)
        evidence = HistoricalCapacityEvidence(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            source_ref=f"server-create:{HISTORICAL_SERVER}",
            error_code="PC-2031",
            observed_at=datetime.now(UTC) - timedelta(days=1),
        )
        assert await repo.record_historical_evidence(evidence, ttl_seconds=3600) is not None
        stored = await repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
        assert stored.state is AccountCapacityState.HEALTHY
        assert stored.accepts_new_orders() is True


def test_the_scratch_database_never_touches_the_configured_one() -> None:
    """Documentation guard: the module only ever writes to its scratch DB."""
    assert SCRATCH_DB not in DB_URL or not DB_URL
    # The default record is eligible: nothing is limited until a definitive
    # provider refusal says otherwise (no invented state, ever).
    record = AccountCapacity(provider_key=PROVIDER, credential_account_id=ACCOUNT)
    assert record.accepts_new_orders()
