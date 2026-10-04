"""Opt-in receipt/pin invariants on real, migrated scratch PostgreSQL.

Missing CLOUD_PLATFORM_TEST_POSTGRES_URL means missing PostgreSQL evidence,
not a passing concurrency gate. No provider traffic is involved. The configured
database is only an administrative connection target; this module creates and
later drops its own uniquely named database, never a pre-existing database.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cloud_platform.db.base import Hold as HoldRow
from cloud_platform.db.base import LedgerEntry as LedgerRow
from cloud_platform.db.base import Operation as OperationRow
from cloud_platform.db.base import Provider as ProviderRow
from cloud_platform.db.base import ProviderAccount as ProviderAccountRow
from cloud_platform.db.base import ProviderOrder as OrderRow
from cloud_platform.db.base import SellableOffer as OfferRow
from cloud_platform.db.base import Server as ServerRow
from cloud_platform.db.base import ServerPriceSnapshot as SnapshotRow
from cloud_platform.db.base import User as UserRow
from cloud_platform.db.base import Wallet as WalletRow
from cloud_platform.modules.operations.create_attempts import (
    CreateAttemptConflict,
    current_account,
    refused_accounts,
)
from cloud_platform.modules.operations.create_attempts_repository import (
    SqlAlchemyCreateAccountAttemptRepository,
)
from cloud_platform.modules.operations.domain import Operation, OperationStatus
from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
PREREQUISITE = "real PostgreSQL evidence requires CLOUD_PLATFORM_TEST_POSTGRES_URL"
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not DB_URL, reason=PREREQUISITE)]
ACCOUNT_A = "hz-main"
ACCOUNT_B = "hz-next"
PROVIDER_IDENTITY = "123456"


@contextmanager
def _scratch_database() -> Iterator[str]:
    name = f"cloud_platform_hz_attempt_{uuid4().hex}"

    async def change_database(*, create: bool) -> None:
        engine = create_async_engine(
            DB_URL,
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
        )
        try:
            async with engine.connect() as connection:
                # The identifier is generated here, never supplied by configuration.
                statement = (
                    f'CREATE DATABASE "{name}"'
                    if create
                    else f'DROP DATABASE "{name}" WITH (FORCE)'
                )
                await connection.execute(sa.text(statement))
        except Exception:
            # Do not expose connection hosts, credentials or driver diagnostics.
            raise RuntimeError("scratch PostgreSQL database administration failed") from None
        finally:
            await engine.dispose()

    # No DROP IF EXISTS, no fixed database name, and no cleanup unless CREATE
    # returned successfully. A failed CREATE cannot authorize dropping anything.
    asyncio.run(change_database(create=True))
    try:
        url = make_url(DB_URL).set(database=name).render_as_string(hide_password=False)
        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=Path(__file__).resolve().parents[2],
            env=dict(os.environ, DATABASE_URL=url),
            capture_output=True,
            text=True,
            timeout=600,
        )
        assert result.returncode == 0, "scratch PostgreSQL schema migration failed"
        yield url
    finally:
        asyncio.run(change_database(create=False))


@pytest.fixture(scope="module")
def scratch_url() -> Iterator[str]:
    if not DB_URL:
        pytest.skip(PREREQUISITE)
    with _scratch_database() as url:
        yield url


@dataclass(frozen=True)
class Environment:
    factory: async_sessionmaker[AsyncSession]
    repo: SqlAlchemyCreateAccountAttemptRepository


@pytest.fixture
async def environment(scratch_url: str) -> AsyncIterator[Environment]:
    engine = create_async_engine(scratch_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield Environment(factory, SqlAlchemyCreateAccountAttemptRepository(factory))
    finally:
        # Connections and disposal stay in the same owning async loop.
        await engine.dispose()


@dataclass(frozen=True)
class Intent:
    operation_id: UUID
    server_id: UUID
    order_id: UUID | None
    wallet_id: UUID
    hold_id: UUID


async def _seed(environment: Environment, *, kind: str = "monthly") -> Intent:
    """One request, one immutable snapshot and one reservation per scenario."""
    user_id, account_id, server_id, operation_id, order_id, wallet_id, hold_id = (
        uuid4() for _ in range(7)
    )
    billing_model = "prepaid_monthly_fixed" if kind == "monthly" else "hourly"
    operation_key = (
        f"order-create:{server_id}" if kind == "monthly" else f"server-create:{server_id}"
    )
    async with environment.factory() as session:
        provider = await session.scalar(sa.select(ProviderRow).where(ProviderRow.name == "hetzner"))
        if provider is None:
            provider = ProviderRow(id=uuid4(), name="hetzner")
            session.add(provider)
            await session.flush()
        # Reuse the one logical offer per product/location/billing model. Each
        # scenario gets its own intent, not a duplicate storefront path.
        offer = await session.scalar(
            sa.select(OfferRow).where(
                OfferRow.provider_key == "hetzner",
                OfferRow.product_id == "cx22",
                OfferRow.location_id == "nbg1",
                OfferRow.billing_model == billing_model,
            )
        )
        if offer is None:
            offer = OfferRow(
                id=uuid4(),
                provider_key="hetzner",
                product_id="cx22",
                location_id="nbg1",
                name="CX22",
                billing_model=billing_model,
                provider_account_id=ACCOUNT_A,
                provider_cost_minor=450,
                provider_cost_currency="EUR",
                selling_price_minor=700,
                selling_currency="USD",
                billing_parameters={"contract_term": "1", "billing_cycle": "1"},
                pricing_metadata={"provider_rate_exact": "4.500000"},
            )
            session.add(offer)
            await session.flush()
        fingerprint: dict[str, Any] = {
            "provider_key": "hetzner",
            "provider_account_id": ACCOUNT_A,
            "product_id": "cx22",
            "location_id": "nbg1",
            "image_id": "ubuntu-24.04",
            "selling_price_minor": 700,
            "selling_currency": "USD",
            "provider_cost_minor": 450,
            "provider_cost_currency": "EUR",
            "provider_rate_exact": "4.500000",
            "offer_id": str(offer.id),
            "billing_model": billing_model,
            "root_disk_size_gb": 40,
            "root_disk_storage_type": "local",
            "billing_parameters": dict(offer.billing_parameters),
            "pricing_metadata": dict(offer.pricing_metadata),
            "technical_metadata": {},
        }
        if kind == "hourly":
            fingerprint.update(fingerprint_version=3, fulfillment_policy="capacity_failover")
        session.add(UserRow(id=user_id, username=user_id.hex, email=f"{user_id.hex}@example.test"))
        await session.flush()
        session.add_all(
            [
                ProviderAccountRow(id=account_id, provider_id=provider.id, user_id=user_id),
                WalletRow(id=wallet_id, user_id=user_id, balance=10000, currency="USD"),
            ]
        )
        await session.flush()
        session.add(
            ServerRow(
                id=server_id,
                user_id=user_id,
                provider_id=provider.id,
                provider_account_id=account_id,
                credential_account_id=ACCOUNT_A,
                state="requested",
                billing_model=billing_model,
                price_per_quantum=700,
                quantum_seconds=3600,
                currency="USD",
                image_id="ubuntu-24.04",
                os="Ubuntu 24.04",
                offer_fingerprint=fingerprint,
                idempotency_key=f"checkout:{server_id}",
            )
        )
        await session.flush()
        session.add_all(
            [
                HoldRow(
                    id=hold_id,
                    wallet_id=wallet_id,
                    amount=700,
                    currency="USD",
                    idempotency_key=f"create:{server_id}",
                    status="created",
                ),
                SnapshotRow(
                    id=uuid4(),
                    server_id=server_id,
                    provider_key="hetzner",
                    plan_id="cx22",
                    location_id="nbg1",
                    currency="EUR",
                    selling_currency="USD",
                    cost_minor=450,
                    selling_minor=700,
                    provider_rate_exact="4.500000",
                    pricing_metadata={"native_currency": "EUR"},
                    offer_fingerprint=deepcopy(fingerprint),
                    book_name="staging",
                    book_version=7,
                    margin_rule={"fixed_minor": 250},
                    priced_at=datetime.now(UTC),
                ),
                OperationRow(
                    id=operation_id,
                    operation_key=operation_key,
                    operation_type="order_create" if kind == "monthly" else "server_create",
                    resource_type="server_order" if kind == "monthly" else "cloud_server",
                    resource_id=order_id if kind == "monthly" else server_id,
                    provider_key="hetzner",
                    status="pending",
                    attempts=0,
                    provider_response={"idempotency_key": operation_key},
                ),
            ]
        )
        if kind == "monthly":
            session.add(
                OrderRow(
                    id=order_id,
                    server_id=server_id,
                    offer_id=offer.id,
                    operation_key=operation_key,
                    provider_key="hetzner",
                    credential_account_id=ACCOUNT_A,
                    status="pending_submit",
                    product_id="cx22",
                    location_id="nbg1",
                    os_name="Ubuntu 24.04",
                    control_panel="None",
                    contract_term="1",
                    billing_cycle="1",
                    provider_cost_minor=450,
                    provider_cost_currency="EUR",
                    selling_price_minor=700,
                    selling_currency="USD",
                    provider_monthly_rate_exact="4.500000",
                    pricing_metadata={"native_currency": "EUR"},
                    settlement_status="pending",
                )
            )
        await session.commit()
    return Intent(
        operation_id, server_id, order_id if kind == "monthly" else None, wallet_id, hold_id
    )


def _columns(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        column.key: deepcopy(getattr(row, column.key)) for column in sa.inspect(type(row)).columns
    }


async def _read(environment: Environment, intent: Intent) -> dict[str, Any]:
    # A single committed join observes receipt, pins and identities together.
    async with environment.factory() as session:
        rows = (
            await session.execute(
                sa.select(OperationRow, ServerRow, OrderRow, SnapshotRow, WalletRow, HoldRow)
                .select_from(OperationRow)
                .join(ServerRow, ServerRow.id == intent.server_id)
                .outerjoin(OrderRow, OrderRow.server_id == ServerRow.id)
                .join(SnapshotRow, SnapshotRow.server_id == ServerRow.id)
                .join(WalletRow, WalletRow.id == intent.wallet_id)
                .join(HoldRow, HoldRow.id == intent.hold_id)
                .where(OperationRow.id == intent.operation_id)
            )
        ).one()
        result = dict(
            zip(
                ("operation", "server", "order", "snapshot", "wallet", "hold"),
                (_columns(row) for row in rows),
                strict=True,
            )
        )
        result["ledger_count"] = await session.scalar(
            sa.select(sa.func.count())
            .select_from(LedgerRow)
            .where(LedgerRow.wallet_id == intent.wallet_id)
        )
        return result


def _immutable(bundle: dict[str, Any]) -> dict[str, Any]:
    excluded = {
        "operation": {"status", "provider_response", "error", "attempts", "updated_at"},
        "server": {
            "credential_account_id",
            "provider_server_id",
            "state",
            "ipv4",
            "ipv6",
            "updated_at",
        },
        "order": {
            "credential_account_id",
            "provider_order_id",
            "status",
            "error",
            "attempts",
            "post_attempted_at",
            "updated_at",
        },
    }
    return {
        key: (
            {field: value for field, value in row.items() if field not in excluded.get(key, set())}
            if isinstance(row, dict)
            else row
        )
        for key, row in bundle.items()
    }


async def _claim(environment: Environment, intent: Intent) -> Operation:
    claimed = await environment.repo.claim(
        intent.operation_id,
        intent.server_id,
        order_id=intent.order_id,
        catalog_account_id=ACCOUNT_A,
    )
    assert claimed is not None
    return claimed


async def _sent(environment: Environment, intent: Intent) -> Operation:
    claimed = await _claim(environment, intent)
    return await environment.repo.start_attempt(intent.operation_id, claimed.attempts, ACCOUNT_A)


async def _refused(environment: Environment, intent: Intent) -> Operation:
    sent = await _sent(environment, intent)
    return await environment.repo.record_refusal(
        intent.operation_id,
        sent.attempts,
        ACCOUNT_A,
        capacity=True,
        error_code="resource_limit_exceeded",
        quota_names=("project_limit",),
    )


def _receipt(bundle: dict[str, Any]) -> dict[str, Any]:
    return bundle["operation"]["provider_response"]["create_routing"]


def _assert_pins(bundle: dict[str, Any], account: str, identity: str | None = None) -> None:
    assert bundle["server"]["credential_account_id"] == account
    assert bundle["server"]["provider_server_id"] == identity
    if bundle["order"] is not None:
        assert bundle["order"]["credential_account_id"] == account
        assert bundle["order"]["provider_order_id"] == identity


@pytest.mark.parametrize("kind", ["monthly", "hourly"])
async def test_only_one_claim_and_sent_attempt_can_win(environment: Environment, kind: str) -> None:
    intent = await _seed(environment, kind=kind)
    original = _immutable(await _read(environment, intent))
    gate = asyncio.Event()

    async def claim() -> Operation | None:
        await gate.wait()
        return await SqlAlchemyCreateAccountAttemptRepository(environment.factory).claim(
            intent.operation_id,
            intent.server_id,
            order_id=intent.order_id,
            catalog_account_id=ACCOUNT_A,
        )

    tasks = [asyncio.create_task(claim()) for _ in range(2)]
    gate.set()
    claims = await asyncio.gather(*tasks)
    owners = [claim for claim in claims if claim is not None]
    assert len(owners) == 1 and claims.count(None) == 1
    owner = owners[0]
    assert owner.attempts == 1 and owner.status is OperationStatus.IN_FLIGHT
    gate.clear()

    async def start(account: str) -> Operation:
        await gate.wait()
        return await SqlAlchemyCreateAccountAttemptRepository(environment.factory).start_attempt(
            intent.operation_id,
            owner.attempts,
            account,
        )

    tasks = [asyncio.create_task(start(account)) for account in (ACCOUNT_A, ACCOUNT_B)]
    gate.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert sum(isinstance(result, Operation) for result in outcomes) == 1
    assert sum(isinstance(result, CreateAttemptConflict) for result in outcomes) == 1
    persisted = await _read(environment, intent)
    attempts = _receipt(persisted)["attempts"]
    assert len(attempts) == 1 and attempts[0]["sequence"] == 1 and attempts[0]["phase"] == "sent"
    _assert_pins(persisted, attempts[0]["account_id"])
    assert persisted["operation"]["attempts"] == 1
    assert _immutable(persisted) == original
    if kind == "monthly":
        assert persisted["order"]["post_attempted_at"] is not None


@pytest.mark.parametrize(
    "case",
    [
        "wrong_operation",
        "wrong_server",
        "wrong_order",
        "wrong_resource_type",
        "wrong_operation_type",
        "wrong_resource_id",
        "wrong_operation_provider",
        "wrong_order_provider",
        "wrong_order_key",
        "different_order_pin",
        "server_not_creating",
        "order_not_submitting",
        "server_already_attached",
        "order_already_attached",
        "wrong_catalog_account",
        "missing_catalog_account",
    ],
)
async def test_claim_rejects_mismatched_or_closed_bindings(
    environment: Environment, case: str
) -> None:
    intent = await _seed(environment)
    operation_id, server_id, order_id, catalog_account = (
        intent.operation_id,
        intent.server_id,
        intent.order_id,
        ACCOUNT_A,
    )
    async with environment.factory() as session:
        operation = await session.get(OperationRow, intent.operation_id)
        server = await session.get(ServerRow, intent.server_id)
        order = await session.get(OrderRow, intent.order_id)
        assert operation is not None and server is not None and order is not None
        if case == "wrong_operation":
            operation_id = uuid4()
        elif case == "wrong_server":
            server_id = uuid4()
        elif case == "wrong_order":
            order_id = uuid4()
        elif case == "wrong_resource_type":
            operation.resource_type = "cloud_server"
        elif case == "wrong_operation_type":
            operation.operation_type = "server_delete"
        elif case == "wrong_resource_id":
            operation.resource_id = uuid4()
        elif case == "wrong_operation_provider":
            operation.provider_key = "leaseweb"
        elif case == "wrong_order_provider":
            order.provider_key = "leaseweb"
        elif case == "wrong_order_key":
            order.operation_key = f"unrelated:{uuid4()}"
        elif case == "different_order_pin":
            order.credential_account_id = ACCOUNT_B
        elif case == "server_not_creating":
            server.state = "active"
        elif case == "order_not_submitting":
            order.status = "submitted"
        elif case == "server_already_attached":
            server.provider_server_id = PROVIDER_IDENTITY
        elif case == "order_already_attached":
            order.provider_order_id = PROVIDER_IDENTITY
        elif case == "wrong_catalog_account":
            catalog_account = ACCOUNT_B
        elif case == "missing_catalog_account":
            catalog_account = None
        else:
            raise AssertionError("unknown binding scenario")
        await session.commit()
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.claim(
            operation_id, server_id, order_id=order_id, catalog_account_id=catalog_account
        )
    assert await _read(environment, intent) == before


@pytest.mark.parametrize(
    "version,policy", [(1, "capacity_failover"), (2, "capacity_failover"), (3, "pinned"), (3, None)]
)
async def test_pinned_hourly_contract_cannot_opt_into_pool(
    environment: Environment,
    version: int,
    policy: str | None,
) -> None:
    intent = await _seed(environment, kind="hourly")
    async with environment.factory() as session:
        server = await session.get(ServerRow, intent.server_id)
        snapshot = await session.scalar(
            sa.select(SnapshotRow).where(SnapshotRow.server_id == intent.server_id)
        )
        assert server is not None and snapshot is not None
        fingerprint = dict(server.offer_fingerprint)
        fingerprint["fingerprint_version"] = version
        if policy is None:
            fingerprint.pop("fulfillment_policy")
        else:
            fingerprint["fulfillment_policy"] = policy
        fingerprint["credential_account_id"] = ACCOUNT_A
        server.offer_fingerprint = fingerprint
        snapshot.offer_fingerprint = deepcopy(fingerprint)
        await session.commit()
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await _claim(environment, intent)
    assert await _read(environment, intent) == before


@pytest.mark.parametrize("phase", ["sent", "outcome_unknown"])
@pytest.mark.parametrize("kind", ["monthly", "hourly"])
async def test_sent_and_unknown_allow_only_same_account_read_only_recovery(
    environment: Environment,
    phase: str,
    kind: str,
) -> None:
    intent = await _seed(environment, kind=kind)
    original = _immutable(await _read(environment, intent))
    sent = await _sent(environment, intent)
    if phase == "outcome_unknown":
        await environment.repo.record_unknown(intent.operation_id, sent.attempts, ACCOUNT_A)
    before = await _read(environment, intent)
    for status in (OperationStatus.PENDING, OperationStatus.FAILED, OperationStatus.COMPLETED):
        with pytest.raises(CreateAttemptConflict):
            await environment.repo.save_outcome(intent.operation_id, sent.attempts, status)
        assert await _read(environment, intent) == before
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, sent.attempts, ACCOUNT_B)
    assert await _read(environment, intent) == before

    recovered = await environment.repo.resume_safe_claim(intent.operation_id, sent.attempts)
    assert recovered.status is OperationStatus.OUTCOME_UNKNOWN
    assert recovered.attempts == sent.attempts + 1
    assert current_account(recovered, ACCOUNT_A) == ACCOUNT_A
    pending = await _read(environment, intent)
    _assert_pins(pending, ACCOUNT_A)
    assert _receipt(pending)["attempts"] == [
        {"sequence": 1, "account_id": ACCOUNT_A, "phase": "outcome_unknown"},
    ]
    assert _immutable(pending) == original
    assert pending["hold"]["status"] == "created" and pending["ledger_count"] == 0
    assert (
        await environment.repo.claim(
            intent.operation_id,
            intent.server_id,
            order_id=intent.order_id,
            catalog_account_id=ACCOUNT_A,
        )
        is None
    )
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, recovered.attempts, ACCOUNT_B)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.record_acceptance(
            intent.operation_id, recovered.attempts, ACCOUNT_B, PROVIDER_IDENTITY
        )
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.record_acceptance(
            intent.operation_id, sent.attempts, ACCOUNT_A, PROVIDER_IDENTITY
        )
    assert await _read(environment, intent) == pending
    await environment.repo.record_acceptance(
        intent.operation_id, recovered.attempts, ACCOUNT_A, PROVIDER_IDENTITY
    )
    accepted = await _read(environment, intent)
    _assert_pins(accepted, ACCOUNT_A, PROVIDER_IDENTITY)
    assert _receipt(accepted)["accepted_account_id"] == ACCOUNT_A
    assert _immutable(accepted) == original


async def test_refusal_rebind_is_atomic_and_fences_the_old_claim(environment: Environment) -> None:
    intent = await _seed(environment)
    original = _immutable(await _read(environment, intent))
    refused = await _refused(environment, intent)
    refusal = await _read(environment, intent)
    assert refused_accounts(refused) == frozenset({ACCOUNT_A})
    assert _receipt(refusal)["attempts"] == [
        {
            "sequence": 1,
            "account_id": ACCOUNT_A,
            "phase": "capacity_refused",
            "error_code": "resource_limit_exceeded",
            "quota_names": ["project_limit"],
        }
    ]
    resumed = await environment.repo.resume_safe_claim(intent.operation_id, refused.attempts)
    assert resumed.status is OperationStatus.PENDING and resumed.attempts == refused.attempts + 1
    # A restarted repository has no in-memory routing history to lean on.
    restarted = Environment(
        environment.factory, SqlAlchemyCreateAccountAttemptRepository(environment.factory)
    )
    claimed = await _claim(restarted, intent)
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await restarted.repo.start_attempt(intent.operation_id, claimed.attempts, ACCOUNT_A)
    assert await _read(environment, intent) == before

    # Reject the order pin in PostgreSQL AFTER the ORM has staged the bundle.
    # This is a real transaction failure, not a mocked session/commit result.
    name = f"hz_rebind_fault_{uuid4().hex}"
    async with environment.factory() as session:
        await session.execute(
            sa.text(f"""
            CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.id = '{intent.order_id}'::uuid
                   AND NEW.credential_account_id = '{ACCOUNT_B}' THEN
                    RAISE EXCEPTION 'intentional scratch rebind constraint' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
        """)
        )
        await session.execute(
            sa.text(f"""
            CREATE TRIGGER {name} BEFORE UPDATE ON provider_orders
            FOR EACH ROW EXECUTE FUNCTION {name}()
        """)
        )
        await session.commit()
    try:
        with pytest.raises(IntegrityError):
            await restarted.repo.start_attempt(intent.operation_id, claimed.attempts, ACCOUNT_B)
        assert await _read(environment, intent) == before
    finally:
        async with environment.factory() as session:
            await session.execute(sa.text(f"DROP TRIGGER {name} ON provider_orders"))
            await session.execute(sa.text(f"DROP FUNCTION {name}()"))
            await session.commit()
    await restarted.repo.start_attempt(intent.operation_id, claimed.attempts, ACCOUNT_B)
    rebound = await _read(environment, intent)
    _assert_pins(rebound, ACCOUNT_B)
    assert _receipt(rebound)["attempts"] == [
        _receipt(refusal)["attempts"][0],
        {"sequence": 2, "account_id": ACCOUNT_B, "phase": "sent"},
    ]
    assert rebound["operation"]["attempts"] == claimed.attempts == refused.attempts + 2
    assert rebound["order"]["post_attempted_at"] is not None
    assert _immutable(rebound) == original
    assert rebound["hold"]["status"] == "created" and rebound["ledger_count"] == 0
    with pytest.raises(CreateAttemptConflict):
        await restarted.repo.record_acceptance(
            intent.operation_id, refused.attempts, ACCOUNT_A, PROVIDER_IDENTITY
        )
    assert await _read(environment, intent) == rebound


@pytest.mark.parametrize(
    "action", ["start", "refuse", "unknown", "accept", "complete", "fail", "requeue", "resume"]
)
async def test_resumed_generation_rejects_every_stale_writer(
    environment: Environment, action: str
) -> None:
    intent = await _seed(environment)
    refused = await _refused(environment, intent)
    await environment.repo.resume_safe_claim(intent.operation_id, refused.attempts)
    live = await _claim(environment, intent)
    await environment.repo.start_attempt(intent.operation_id, live.attempts, ACCOUNT_B)
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        if action == "start":
            await environment.repo.start_attempt(intent.operation_id, refused.attempts, "hz-third")
        elif action == "refuse":
            await environment.repo.record_refusal(
                intent.operation_id,
                refused.attempts,
                ACCOUNT_B,
                capacity=True,
                error_code="resource_limit_exceeded",
            )
        elif action == "unknown":
            await environment.repo.record_unknown(intent.operation_id, refused.attempts, ACCOUNT_B)
        elif action == "accept":
            await environment.repo.record_acceptance(
                intent.operation_id, refused.attempts, ACCOUNT_B, PROVIDER_IDENTITY
            )
        elif action == "complete":
            await environment.repo.save_outcome(
                intent.operation_id,
                refused.attempts,
                OperationStatus.COMPLETED,
                correlation={"provider_server_id": PROVIDER_IDENTITY},
            )
        elif action == "fail":
            await environment.repo.save_outcome(
                intent.operation_id, refused.attempts, OperationStatus.FAILED
            )
        elif action == "requeue":
            await environment.repo.save_outcome(
                intent.operation_id, refused.attempts, OperationStatus.PENDING
            )
        elif action == "resume":
            await environment.repo.resume_safe_claim(intent.operation_id, refused.attempts)
        else:
            raise AssertionError("unknown stale-writer scenario")
    assert await _read(environment, intent) == before
    assert before["operation"]["attempts"] == live.attempts
    _assert_pins(before, ACCOUNT_B)


@pytest.mark.parametrize("kind", ["monthly", "hourly"])
async def test_accepted_resource_cannot_be_rebound(environment: Environment, kind: str) -> None:
    intent = await _seed(environment, kind=kind)
    original = _immutable(await _read(environment, intent))
    refused = await _refused(environment, intent)
    await environment.repo.resume_safe_claim(intent.operation_id, refused.attempts)
    live = await _claim(environment, intent)
    await environment.repo.start_attempt(intent.operation_id, live.attempts, ACCOUNT_B)
    await environment.repo.record_acceptance(
        intent.operation_id, live.attempts, ACCOUNT_B, PROVIDER_IDENTITY
    )
    accepted = await _read(environment, intent)
    _assert_pins(accepted, ACCOUNT_B, PROVIDER_IDENTITY)
    receipt = _receipt(accepted)
    assert (
        receipt["catalog_account_id"] == ACCOUNT_A and receipt["accepted_account_id"] == ACCOUNT_B
    )
    assert receipt["attempts"] == [
        {
            "sequence": 1,
            "account_id": ACCOUNT_A,
            "phase": "capacity_refused",
            "error_code": "resource_limit_exceeded",
            "quota_names": ["project_limit"],
        },
        {
            "sequence": 2,
            "account_id": ACCOUNT_B,
            "phase": "accepted",
            "provider_server_id": PROVIDER_IDENTITY,
        },
    ]
    assert accepted["operation"]["provider_response"]["provider_server_id"] == PROVIDER_IDENTITY
    if kind == "monthly":
        assert accepted["order"]["status"] == "submitted"
    assert _immutable(accepted) == original
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.record_acceptance(
            intent.operation_id, live.attempts, ACCOUNT_B, "654321"
        )
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.record_acceptance(
            intent.operation_id, live.attempts, ACCOUNT_A, PROVIDER_IDENTITY
        )
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, live.attempts, ACCOUNT_A)
    for status in (OperationStatus.PENDING, OperationStatus.FAILED):
        with pytest.raises(CreateAttemptConflict):
            await environment.repo.save_outcome(intent.operation_id, live.attempts, status)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.save_outcome(
            intent.operation_id,
            live.attempts,
            OperationStatus.COMPLETED,
            correlation={"provider_server_id": "654321"},
        )
    assert await _read(environment, intent) == accepted
    await environment.repo.save_outcome(
        intent.operation_id,
        live.attempts,
        OperationStatus.COMPLETED,
        correlation={"provider_server_id": PROVIDER_IDENTITY, "recovered": True},
    )
    completed = await _read(environment, intent)
    assert completed["operation"]["status"] == "completed"
    assert _receipt(completed) == receipt
    assert completed["server"]["state"] == ("requested" if kind == "monthly" else "provisioning")
    assert (
        completed["operation"]["provider_response"]["idempotency_key"]
        == accepted["operation"]["operation_key"]
    )
    assert completed["operation"]["provider_response"]["recovered"] is True
    _assert_pins(completed, ACCOUNT_B, PROVIDER_IDENTITY)
    assert _immutable(completed) == original
    assert (
        await environment.repo.claim(
            intent.operation_id,
            intent.server_id,
            order_id=intent.order_id,
            catalog_account_id=ACCOUNT_A,
        )
        is None
    )
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, live.attempts, ACCOUNT_A)
    assert await _read(environment, intent) == completed


@pytest.mark.parametrize(
    "case", ["claim_generation", "post_marker", "provider_identity", "invalid_receipt"]
)
async def test_historical_attempt_without_proof_never_becomes_fresh_create(
    environment: Environment,
    case: str,
) -> None:
    intent = await _seed(environment)
    async with environment.factory() as session:
        operation = await session.get(OperationRow, intent.operation_id)
        order = await session.get(OrderRow, intent.order_id)
        assert operation is not None and order is not None
        if case == "claim_generation":
            operation.attempts = 1
        elif case == "post_marker":
            order.post_attempted_at = datetime.now(UTC)
        elif case == "provider_identity":
            operation.provider_response = {"provider_server_id": PROVIDER_IDENTITY}
        elif case == "invalid_receipt":
            operation.provider_response = {"create_routing": {"version": 0, "attempts": []}}
        else:
            raise AssertionError("unknown historical scenario")
        await session.commit()
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await _claim(environment, intent)
    assert await _read(environment, intent) == before
    _assert_pins(before, ACCOUNT_A)
    assert before["hold"]["status"] == "created" and before["ledger_count"] == 0


@pytest.mark.parametrize("error_code", [None, "", "resource limit exceeded"])
async def test_capacity_refusal_requires_safe_documented_evidence(
    environment: Environment,
    error_code: str | None,
) -> None:
    intent = await _seed(environment)
    sent = await _sent(environment, intent)
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.record_refusal(
            intent.operation_id, sent.attempts, ACCOUNT_A, capacity=True, error_code=error_code
        )
    assert await _read(environment, intent) == before
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, sent.attempts, ACCOUNT_B)
    assert await _read(environment, intent) == before


async def test_non_capacity_refusal_cannot_authorize_another_account(
    environment: Environment,
) -> None:
    intent = await _seed(environment)
    sent = await _sent(environment, intent)
    await environment.repo.record_refusal(
        intent.operation_id, sent.attempts, ACCOUNT_A, capacity=False, error_code="invalid_input"
    )
    before = await _read(environment, intent)
    assert _receipt(before)["attempts"] == [
        {
            "sequence": 1,
            "account_id": ACCOUNT_A,
            "phase": "refused",
            "error_code": "invalid_input",
            "quota_names": [],
        },
    ]
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, sent.attempts, ACCOUNT_B)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.save_outcome(
            intent.operation_id, sent.attempts, OperationStatus.PENDING
        )
    assert await _read(environment, intent) == before
    recovered = await environment.repo.resume_safe_claim(intent.operation_id, sent.attempts)
    assert recovered.attempts == sent.attempts + 1
    assert recovered.status is OperationStatus.IN_FLIGHT
    assert _receipt(await _read(environment, intent)) == _receipt(before)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.save_outcome(
            intent.operation_id, sent.attempts, OperationStatus.FAILED
        )
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, recovered.attempts, ACCOUNT_B)
    await environment.repo.save_outcome(
        intent.operation_id,
        recovered.attempts,
        OperationStatus.FAILED,
        error="definitive provider refusal",
    )
    failed = await _read(environment, intent)
    assert failed["operation"]["status"] == "failed" and _receipt(failed) == _receipt(before)
    _assert_pins(failed, ACCOUNT_A)
    assert _immutable(failed) == _immutable(before)


@pytest.mark.parametrize("kind", ["monthly", "hourly"])
async def test_initialized_receipt_without_sent_safely_restarts(
    environment: Environment,
    kind: str,
) -> None:
    intent = await _seed(environment, kind=kind)
    original = _immutable(await _read(environment, intent))
    old = await _claim(environment, intent)
    initialized = await _read(environment, intent)
    assert _receipt(initialized)["attempts"] == []
    _assert_pins(initialized, ACCOUNT_A)
    if kind == "monthly":
        assert initialized["order"]["post_attempted_at"] is None
    resumed = await environment.repo.resume_safe_claim(intent.operation_id, old.attempts)
    assert resumed.status is OperationStatus.PENDING and resumed.attempts == old.attempts + 1
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.start_attempt(intent.operation_id, old.attempts, ACCOUNT_A)
    assert await _read(environment, intent) == before
    restarted = Environment(
        environment.factory,
        SqlAlchemyCreateAccountAttemptRepository(environment.factory),
    )
    live = await _claim(restarted, intent)
    await restarted.repo.start_attempt(intent.operation_id, live.attempts, ACCOUNT_B)
    sent = await _read(environment, intent)
    _assert_pins(sent, ACCOUNT_B)
    assert _receipt(sent)["attempts"] == [
        {"sequence": 1, "account_id": ACCOUNT_B, "phase": "sent"},
    ]
    assert sent["operation"]["attempts"] == old.attempts + 2
    assert _immutable(sent) == original


@pytest.mark.parametrize("binding", ["server", "order"])
@pytest.mark.parametrize(
    "phase,action",
    [
        ("capacity_refused", "start"),
        ("sent", "refuse"),
        ("sent", "unknown"),
        ("sent", "accept"),
        ("sent", "finalize_unknown"),
        ("sent", "resume"),
    ],
)
async def test_receipt_owner_drift_blocks_fenced_writes(
    environment: Environment,
    binding: str,
    phase: str,
    action: str,
) -> None:
    intent = await _seed(environment)
    live = (
        await _refused(environment, intent)
        if phase == "capacity_refused"
        else await _sent(environment, intent)
    )
    async with environment.factory() as session:
        row = await session.get(
            ServerRow if binding == "server" else OrderRow,
            intent.server_id if binding == "server" else intent.order_id,
        )
        assert row is not None
        row.credential_account_id = ACCOUNT_B
        await session.commit()
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        if action == "start":
            await environment.repo.start_attempt(intent.operation_id, live.attempts, ACCOUNT_B)
        elif action == "refuse":
            await environment.repo.record_refusal(
                intent.operation_id,
                live.attempts,
                ACCOUNT_A,
                capacity=True,
                error_code="resource_limit_exceeded",
            )
        elif action == "unknown":
            await environment.repo.record_unknown(intent.operation_id, live.attempts, ACCOUNT_A)
        elif action == "accept":
            await environment.repo.record_acceptance(
                intent.operation_id,
                live.attempts,
                ACCOUNT_A,
                PROVIDER_IDENTITY,
            )
        elif action == "finalize_unknown":
            await environment.repo.save_outcome(
                intent.operation_id,
                live.attempts,
                OperationStatus.OUTCOME_UNKNOWN,
            )
        elif action == "resume":
            await environment.repo.resume_safe_claim(intent.operation_id, live.attempts)
        else:
            raise AssertionError("unknown ownership scenario")
    assert await _read(environment, intent) == before


@pytest.mark.parametrize("case", ["catalog_provenance", "both_execution_pins"])
async def test_reclaim_rejects_receipt_binding_disagreement(
    environment: Environment,
    case: str,
) -> None:
    intent = await _seed(environment)
    refused = await _refused(environment, intent)
    await environment.repo.resume_safe_claim(intent.operation_id, refused.attempts)
    async with environment.factory() as session:
        operation = await session.get(OperationRow, intent.operation_id)
        server = await session.get(ServerRow, intent.server_id)
        order = await session.get(OrderRow, intent.order_id)
        assert operation is not None and server is not None and order is not None
        if case == "catalog_provenance":
            response = deepcopy(operation.provider_response)
            response["create_routing"]["catalog_account_id"] = ACCOUNT_B
            operation.provider_response = response
        else:
            server.credential_account_id = ACCOUNT_B
            order.credential_account_id = ACCOUNT_B
        await session.commit()
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await _claim(environment, intent)
    assert await _read(environment, intent) == before


async def test_failed_acceptance_rolls_back_both_identities_and_receipt(
    environment: Environment,
) -> None:
    intent = await _seed(environment)
    refused = await _refused(environment, intent)
    await environment.repo.resume_safe_claim(intent.operation_id, refused.attempts)
    live = await _claim(environment, intent)
    await environment.repo.start_attempt(intent.operation_id, live.attempts, ACCOUNT_B)
    before = await _read(environment, intent)
    name = f"hz_accept_fault_{uuid4().hex}"
    async with environment.factory() as session:
        await session.execute(
            sa.text(f"""
            CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.id = '{intent.order_id}'::uuid AND NEW.provider_order_id IS NOT NULL THEN
                    RAISE EXCEPTION 'intentional scratch acceptance constraint'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$
        """)
        )
        await session.execute(
            sa.text(f"""
            CREATE TRIGGER {name} BEFORE UPDATE ON provider_orders
            FOR EACH ROW EXECUTE FUNCTION {name}()
        """)
        )
        await session.commit()
    try:
        with pytest.raises(IntegrityError):
            await environment.repo.record_acceptance(
                intent.operation_id,
                live.attempts,
                ACCOUNT_B,
                PROVIDER_IDENTITY,
            )
        assert await _read(environment, intent) == before
        _assert_pins(before, ACCOUNT_B)
        assert _receipt(before)["attempts"][-1]["phase"] == "sent"
    finally:
        async with environment.factory() as session:
            await session.execute(sa.text(f"DROP TRIGGER {name} ON provider_orders"))
            await session.execute(sa.text(f"DROP FUNCTION {name}()"))
            await session.commit()
    restarted = SqlAlchemyCreateAccountAttemptRepository(environment.factory)
    await restarted.record_acceptance(
        intent.operation_id,
        live.attempts,
        ACCOUNT_B,
        PROVIDER_IDENTITY,
    )
    attached = await _read(environment, intent)
    _assert_pins(attached, ACCOUNT_B, PROVIDER_IDENTITY)
    assert _receipt(attached)["accepted_account_id"] == ACCOUNT_B
    assert _receipt(attached)["attempts"][-1] == {
        "sequence": 2,
        "account_id": ACCOUNT_B,
        "phase": "accepted",
        "provider_server_id": PROVIDER_IDENTITY,
    }
    assert _immutable(attached) == _immutable(before)


@pytest.mark.parametrize("case", ["resource_type", "bound_server", "unexpected_order"])
async def test_server_create_binding_cannot_be_reinterpreted(
    environment: Environment,
    case: str,
) -> None:
    intent = await _seed(environment, kind="hourly")
    order_id = None
    async with environment.factory() as session:
        operation = await session.get(OperationRow, intent.operation_id)
        assert operation is not None
        if case == "resource_type":
            operation.resource_type = "server_order"
        elif case == "bound_server":
            operation.resource_id = uuid4()
        else:
            order_id = uuid4()
        await session.commit()
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await environment.repo.claim(
            intent.operation_id,
            intent.server_id,
            order_id=order_id,
            catalog_account_id=ACCOUNT_A,
        )
    assert await _read(environment, intent) == before


@pytest.mark.parametrize(
    "case", ["boolean_version", "boolean_sequence", "duplicate_account", "unnormalized_catalog"]
)
async def test_malformed_persisted_receipt_cannot_authorize_reclaim(
    environment: Environment,
    case: str,
) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    intent = await _seed(environment)
    refused = await _refused(environment, intent)
    await environment.repo.resume_safe_claim(intent.operation_id, refused.attempts)
    async with environment.factory() as session:
        operation = await session.get(OperationRow, intent.operation_id)
        assert operation is not None
        response = deepcopy(operation.provider_response)
        receipt = response["create_routing"]
        if case == "boolean_version":
            receipt["version"] = True
        elif case == "boolean_sequence":
            receipt["attempts"][0]["sequence"] = True
        elif case == "duplicate_account":
            duplicate = deepcopy(receipt["attempts"][0])
            duplicate["sequence"] = 2
            receipt["attempts"].append(duplicate)
        else:
            receipt["catalog_account_id"] = " HZ-MAIN "
        operation.provider_response = response
        # Python considers True == 1; force the malformed JSON write instead
        # of letting ORM equality elide this deliberate corruption fixture.
        flag_modified(operation, "provider_response")
        await session.commit()
    before = await _read(environment, intent)
    with pytest.raises(CreateAttemptConflict):
        await _claim(environment, intent)
    assert await _read(environment, intent) == before


async def test_generic_operation_save_cannot_erase_receipt_or_fence(
    environment: Environment,
) -> None:
    intent = await _seed(environment)
    attempts = SqlAlchemyCreateAccountAttemptRepository(environment.factory)
    owner = await attempts.claim(
        intent.operation_id,
        intent.server_id,
        order_id=intent.order_id,
        catalog_account_id=ACCOUNT_A,
    )
    stale = deepcopy(owner)
    await attempts.start_attempt(intent.operation_id, owner.attempts, ACCOUNT_A)
    before = await _read(environment, intent)
    stale.status = OperationStatus.COMPLETED
    stale.provider_response = {"provider_server_id": "foreign"}
    stale.attempts += 1
    with pytest.raises(CreateAttemptConflict):
        await SqlAlchemyOperationRepository(environment.factory).save(stale)
    assert await _read(environment, intent) == before


async def test_committed_refusal_returns_its_generation_not_a_recovery_owners(
    environment: Environment,
) -> None:
    intent = await _seed(environment)
    sent = await _sent(environment, intent)

    @asynccontextmanager
    async def interleaved_factory() -> AsyncIterator[AsyncSession]:
        async with environment.factory() as session:
            commit = session.commit

            async def commit_then_recover() -> None:
                await commit()
                await environment.repo.resume_safe_claim(intent.operation_id, sent.attempts)

            session.commit = commit_then_recover
            yield session

    original_worker = SqlAlchemyCreateAccountAttemptRepository(interleaved_factory)
    returned = await original_worker.record_refusal(
        intent.operation_id,
        sent.attempts,
        ACCOUNT_A,
        capacity=True,
        error_code="resource_limit_exceeded",
    )
    persisted = await _read(environment, intent)
    assert persisted["operation"]["attempts"] == sent.attempts + 1
    assert returned.attempts == sent.attempts
    assert returned.status is OperationStatus.IN_FLIGHT
    with pytest.raises(CreateAttemptConflict):
        await original_worker.start_attempt(intent.operation_id, returned.attempts, ACCOUNT_B)
    _assert_pins(await _read(environment, intent), ACCOUNT_A)
