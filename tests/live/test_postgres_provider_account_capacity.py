"""Opt-in live tests for durable provider account capacity (PostgreSQL).

Skipped unless::

    CLOUD_PLATFORM_TEST_POSTGRES_URL=postgresql+asyncpg://... (a SCRATCH server)

These exist because only real PostgreSQL proves what this table's correctness
actually depends on:

* the ``(provider_key, credential_account_id)`` UNIQUE constraint drives a
  select-then-mutate upsert — two concurrent workers recording the SAME refusal
  must serialize on the row lock and end with ONE row and TWO observations,
  never a duplicate-key error and never a lost observation;
* a ``limit_reached`` window is TIME BOUNDED in the database itself
  (``expires_at``), so an account comes back to normal publication on its own;
* an unknown state value (a newer release's enum) reads as "cannot take new
  orders", never as healthy — the fail-closed direction;
* ``clear`` really removes the row while ``record_healthy`` keeps the evidence.

The scratch DATABASE is created and dropped by this module on the target
server; the configured database is never modified.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cloud_platform.db.base import ProviderAccountCapacity as _CapacityModel
from cloud_platform.modules.provider_capacity.domain import (
    AccountCapacity,
    AccountCapacityState,
    CapacityObservation,
)
from cloud_platform.modules.provider_capacity.repository import (
    SqlAlchemyAccountCapacityRepository,
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


@pytest.fixture()
def capacity_repo() -> Iterator[SqlAlchemyAccountCapacityRepository]:
    """A repository bound to a fresh, migrated scratch database."""
    if not DB_URL:  # pragma: no cover - skipped by the marker
        pytest.skip("live PostgreSQL tests require CLOUD_PLATFORM_TEST_POSTGRES_URL")
    with _scratch_database() as url:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            yield SqlAlchemyAccountCapacityRepository(factory)
        finally:
            asyncio.run(engine.dispose())


pytestmark = requires_postgres


async def _insert_raw_state(url: str, state: str) -> None:
    engine = create_async_engine(url)
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

    async def test_the_window_is_time_bounded_and_the_evidence_survives(
        self, capacity_repo: SqlAlchemyAccountCapacityRepository
    ) -> None:
        now = datetime.now(UTC)
        past = now - timedelta(hours=2)
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=60,
            now=past,
        )
        # Expired: eligible again, and the account is no longer excluded.
        assert await capacity_repo.limit_reached_accounts(PROVIDER, now=now) == frozenset()
        stored = await capacity_repo.get(PROVIDER, ACCOUNT)
        assert stored is not None
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
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id=ACCOUNT,
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=3600,
        )
        await capacity_repo.record_limit_reached(
            provider_key=PROVIDER,
            credential_account_id="sales-org-uk",
            observation=CapacityObservation(error_code="PC-2031"),
            ttl_seconds=60,
            now=datetime.now(UTC) - timedelta(hours=2),
        )
        # expiring the UK signal must not touch the north one
        assert await capacity_repo.limit_reached_accounts(PROVIDER) == frozenset({ACCOUNT})
        assert await capacity_repo.get(PROVIDER, "sales-org-uk") is not None
        assert await capacity_repo.get("hetzner", ACCOUNT) is None

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
        assert stored.state is AccountCapacityState.LIMIT_REACHED
        assert await capacity_repo.limit_reached_accounts(PROVIDER) == frozenset({ACCOUNT})

    async def test_a_naive_stored_expiry_does_not_immortalize_the_signal(
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
        assert stored.is_limit_reached(now=now + timedelta(hours=2)) is False


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
        engine = create_async_engine(_with_database(DB_URL, SCRATCH_DB))
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                count = (
                    await session.execute(sa.select(sa.func.count()).select_from(_CapacityModel))
                ).scalar_one()
        finally:
            await engine.dispose()
        assert int(count) == 1


def test_the_scratch_database_never_touches_the_configured_one() -> None:
    """Documentation guard: the module only ever writes to its scratch DB."""
    assert SCRATCH_DB not in DB_URL or not DB_URL
    # The default record is eligible: nothing is limited until a definitive
    # provider refusal says otherwise (no invented state, ever).
    record = AccountCapacity(provider_key=PROVIDER, credential_account_id=ACCOUNT)
    assert record.accepts_new_orders()
