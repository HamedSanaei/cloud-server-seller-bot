"""Executable provider boundaries: pinned owners, redacted failures and claim fencing."""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import JSON, MetaData, create_engine, insert, select
from sqlalchemy.orm import Session

from cloud_platform.db.base import Operation as OperationRow
from cloud_platform.db.base import Server as ServerRow
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
from cloud_platform.modules.operations.create_attempts import CreateAttemptConflict, create_routing
from cloud_platform.modules.operations.create_attempts_repository import (
    SqlAlchemyCreateAccountAttemptRepository,
)
from cloud_platform.modules.operations.domain import (
    InvalidOperationTransition,
    OperationStatus,
)
from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
from cloud_platform.modules.servers.models import ServerOperation
from cloud_platform.modules.servers.service import ServerProviderError
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.retry import RetryExecutor, RetryPolicy
from cloud_platform.providers.routing import UnknownCredentialAccountError, provider_for
from tests.unit.test_servers_management_service import CUSTOMER, SERVER_ID, make_service

SECRET = "credential-and-issued-password-must-not-escape"  # pragma: allowlist secret


@pytest.mark.parametrize("account", ["hz-owned", " hz-owned "])
def test_legacy_registry_cannot_substitute_default_for_pinned_owner(account):
    registry = SimpleNamespace(get=MagicMock())
    with pytest.raises(UnknownCredentialAccountError):
        provider_for(registry, "hetzner", account)
    registry.get.assert_not_called()


@pytest.mark.parametrize("account", [None, "", "default", " default "])
def test_legacy_registry_preserves_default_account_management(account):
    owner = object()
    registry = SimpleNamespace(get=MagicMock(return_value=owner))
    assert provider_for(registry, "hetzner", account) is owner
    registry.get.assert_called_once_with("hetzner")


@pytest.mark.parametrize(
    "error_type,attempts",
    [(ProviderAuthError, 1), (ProviderOutcomeUnknown, 1), (ProviderUnavailable, 3)],
)
async def test_retry_logs_categories_not_credential_bearing_exceptions(
    error_type, attempts, caplog
):
    calls = 0
    sleeps = []

    async def operation():
        nonlocal calls
        calls += 1
        raise error_type(SECRET)

    async def sleep(delay):
        sleeps.append(delay)

    executor = RetryExecutor(RetryPolicy(max_attempts=3, jitter=0), sleep_fn=sleep)
    with caplog.at_level(logging.INFO, logger="cloud_platform.providers.retry"):
        with pytest.raises(error_type):
            await executor.execute(operation)
    assert calls == attempts
    assert len(sleeps) == attempts - 1
    assert error_type.__name__ in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ProviderError),
        (409, ProviderConflict),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (422, ProviderError),
    ],
)
async def test_hetzner_errors_never_retain_raw_provider_body(status, expected):
    provider = HetznerCloudProvider(token="fixture-token")
    await provider.close()
    provider._client = httpx.AsyncClient(
        base_url="https://hetzner.invalid/v1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, json={"error": {"code": SECRET, "message": SECRET}}
            )
        ),
    )
    provider._backoff = SimpleNamespace(policy=SimpleNamespace(max_retries=0))
    try:
        with pytest.raises(expected) as raised:
            await provider._request("GET", "/servers")
        assert SECRET not in "".join(traceback.format_exception(raised.value))
    finally:
        await provider.close()


@pytest.mark.parametrize("mutation", [False, True])
async def test_hetzner_transport_traceback_redacts_original_credentials(mutation):
    def fail(request):
        raise httpx.ReadTimeout(SECRET, request=request)

    provider = HetznerCloudProvider(token="fixture-token")
    await provider.close()
    provider._client = httpx.AsyncClient(
        base_url="https://hetzner.invalid/v1", transport=httpx.MockTransport(fail)
    )
    try:
        call = provider._mutation_request if mutation else provider._request
        expected = ProviderOutcomeUnknown if mutation else ProviderUnavailable
        with pytest.raises(expected) as raised:
            await call("POST" if mutation else "GET", "/servers")
        assert SECRET not in "".join(traceback.format_exception(raised.value))
    finally:
        await provider.close()


@pytest.mark.parametrize("inventory", ["servers", "vps"])
@pytest.mark.parametrize(
    "payload",
    [
        {"servers": []},
        {"servers": [], "meta": {"pagination": {"page": 1}}},
        {"servers": [], "meta": {"pagination": {"page": 1, "next_page": None, "total_entries": 1}}},
        {"servers": [{"id": 1}, {"id": 1}], "meta": {"pagination": {"page": 1, "next_page": None}}},
    ],
)
async def test_list_servers_cannot_turn_incomplete_inventory_into_absence(payload, inventory):
    provider = HetznerCloudProvider(token="fixture-token")
    await provider.close()
    provider._client = httpx.AsyncClient(
        base_url="https://hetzner.invalid/v1",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(ProviderError):
            if inventory == "vps":
                await provider.vps_management.list_vps_info()
            else:
                await provider.list_servers()
    finally:
        await provider.close()


async def test_server_mutation_failure_redacts_events_and_exception_traceback():
    service, provider, _, audit, sink = make_service()
    provider.failures["reset_vps_password"] = ProviderError(SECRET)
    token = await service.issue_confirmation(CUSTOMER, SERVER_ID, ServerOperation.PASSWORD_RESET)
    with pytest.raises(ServerProviderError) as raised:
        await service.reset_password(CUSTOMER, SERVER_ID, confirmation_token=token)
    assert provider.count("reset_vps_password") == 1
    assert audit.events and sink.events
    assert SECRET not in repr(audit.events)
    assert SECRET not in repr(sink.events)
    assert SECRET not in "".join(traceback.format_exception(raised.value))


@pytest.fixture
def persisted_operations(tmp_path):
    """Execute production repository SQL; adapt PostgreSQL-only DDL, not queries."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operations.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    table = OperationRow.__table__.to_metadata(MetaData())
    table.c.provider_response.type = JSON()
    table.c.id.server_default = None
    table.create(engine)
    operation_id, resource_id = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            insert(table).values(
                id=operation_id,
                operation_key=f"server-create:{resource_id}",
                operation_type="server_create",
                resource_type="server",
                resource_id=resource_id,
                provider_key="hetzner",
                status="pending",
                attempts=0,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

    class LocalSession:
        def __init__(self):
            self.session = Session(engine, expire_on_commit=False)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await asyncio.to_thread(self.session.close)

        async def execute(self, statement):
            return await asyncio.to_thread(self.session.execute, statement)

        async def scalar(self, statement):
            return await asyncio.to_thread(self.session.scalar, statement)

        async def flush(self):
            await asyncio.to_thread(self.session.flush)

        async def commit(self):
            await asyncio.to_thread(self.session.commit)

        async def refresh(self, row):
            await asyncio.to_thread(self.session.refresh, row)

    yield SqlAlchemyOperationRepository(LocalSession), operation_id
    engine.dispose()


async def test_competing_operation_claims_have_one_winner(persisted_operations):
    repo, operation_id = persisted_operations
    claims = await asyncio.gather(repo.claim(operation_id), repo.claim(operation_id))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    current = await repo.get(operation_id)
    assert current.status is OperationStatus.IN_FLIGHT
    assert current.attempts == winners[0].attempts == 1


async def test_stale_worker_cannot_overwrite_new_claim_or_requeue_it(persisted_operations):
    repo, operation_id = persisted_operations
    first = await repo.claim(operation_id)
    stale = replace(first)
    first.requeue("read-only recovery requested another attempt")
    await repo.save(first)
    second = await repo.claim(operation_id)
    assert second.attempts == 2
    stale.complete({"provider_server_id": "old-resource"})
    with pytest.raises(InvalidOperationTransition):
        await repo.save(stale)
    current = await repo.get(operation_id)
    assert current.status is OperationStatus.IN_FLIGHT
    assert current.attempts == 2
    assert current.provider_response is None
    assert await repo.claim(operation_id) is None


@pytest.fixture
async def receipt_operations(persisted_operations):
    operations, operation_id = persisted_operations
    operation = await operations.get(operation_id)
    async with operations._session_factory() as session:
        engine = session.session.get_bind()
    metadata = MetaData()
    # Resolve original FK targets without creating unrelated tables. SQLite
    # does not enforce those unrelated FK fixtures; receipt queries are unmodified.
    for original in ServerRow.metadata.tables.values():
        original.to_metadata(metadata)
    provider_table, server_table = metadata.tables["providers"], metadata.tables["servers"]
    server_table.c.offer_fingerprint.type = JSON()
    for table in (provider_table, server_table):
        table.c.id.server_default = None
        table.create(engine)
    provider_id = uuid4()
    now = datetime.now(UTC).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            insert(provider_table).values(id=provider_id, name="hetzner", created_at=now)
        )
        connection.execute(
            insert(server_table).values(
                id=operation.resource_id,
                user_id=uuid4(),
                provider_id=provider_id,
                provider_account_id=uuid4(),
                state="requested",
                price_per_quantum=1,
                currency="EUR",
                credential_account_id="hz-a",
                created_at=now,
                updated_at=now,
            )
        )
    return SimpleNamespace(
        attempts=SqlAlchemyCreateAccountAttemptRepository(operations._session_factory),
        operations=operations,
        operation_id=operation_id,
        server_id=operation.resource_id,
    )


async def test_unknown_create_receipt_never_authorizes_another_account(receipt_operations):
    env = receipt_operations
    claim = await env.attempts.claim(env.operation_id, env.server_id)
    await env.attempts.start_attempt(claim.id, claim.attempts, "hz-a")
    await env.attempts.record_unknown(claim.id, claim.attempts, "hz-a")
    with pytest.raises(CreateAttemptConflict):
        await env.attempts.start_attempt(claim.id, claim.attempts, "hz-b")
    with pytest.raises(CreateAttemptConflict):
        await env.attempts.save_outcome(claim.id, claim.attempts, OperationStatus.PENDING)
    recovery = await env.attempts.resume_safe_claim(claim.id, claim.attempts)
    assert recovery.status is OperationStatus.OUTCOME_UNKNOWN
    with pytest.raises(CreateAttemptConflict):
        await env.attempts.record_acceptance(claim.id, claim.attempts, "hz-a", "42")
    with pytest.raises(CreateAttemptConflict):
        await env.attempts.record_acceptance(recovery.id, recovery.attempts, "hz-b", "42")
    await env.attempts.record_acceptance(recovery.id, recovery.attempts, "hz-a", "42")
    completed = await env.attempts.save_outcome(
        recovery.id, recovery.attempts, OperationStatus.COMPLETED
    )
    receipt = create_routing(completed)
    assert receipt["accepted_account_id"] == "hz-a"
    assert [(entry["account_id"], entry["phase"]) for entry in receipt["attempts"]] == [
        ("hz-a", "accepted")
    ]
    async with env.operations._session_factory() as session:
        server = await session.scalar(select(ServerRow).where(ServerRow.id == env.server_id))
        assert (server.credential_account_id, server.provider_server_id, server.state) == (
            "hz-a",
            "42",
            "provisioning",
        )


async def test_capacity_refusal_allows_only_new_owner_with_frozen_request(receipt_operations):
    env = receipt_operations
    claim = await env.attempts.claim(env.operation_id, env.server_id)
    request = {"name": "srv-owned", "plan_id": "cx22", "location_id": "fsn1", "image_id": "ubuntu"}
    await env.attempts.start_attempt(claim.id, claim.attempts, "hz-a", request_facts=request)
    with pytest.raises(CreateAttemptConflict):
        await env.attempts.record_refusal(
            claim.id, claim.attempts, "hz-a", capacity=True, error_code="transient_timeout"
        )
    refusal = await env.attempts.record_refusal(
        claim.id,
        claim.attempts,
        "hz-a",
        capacity=True,
        error_code="resource_limit_exceeded",
        quota_names=("servers", SECRET + "="),
    )
    assert create_routing(refusal)["attempts"][0]["quota_names"] == ["servers"]
    with pytest.raises(CreateAttemptConflict):
        await env.attempts.start_attempt(claim.id, claim.attempts, "hz-a", request_facts=request)
    with pytest.raises(CreateAttemptConflict):
        await env.attempts.start_attempt(
            claim.id, claim.attempts, "hz-b", request_facts={**request, "image_id": "different"}
        )
    await env.attempts.start_attempt(claim.id, claim.attempts, "hz-b", request_facts=request)
    await env.attempts.record_acceptance(claim.id, claim.attempts, "hz-b", "84")
    completed = await env.attempts.save_outcome(claim.id, claim.attempts, OperationStatus.COMPLETED)
    receipt = create_routing(completed)
    assert receipt["accepted_account_id"] == "hz-b"
    assert [(entry["account_id"], entry["phase"]) for entry in receipt["attempts"]] == [
        ("hz-a", "capacity_refused"),
        ("hz-b", "accepted"),
    ]
    async with env.operations._session_factory() as session:
        server = await session.scalar(select(ServerRow).where(ServerRow.id == env.server_id))
        assert server.credential_account_id == "hz-b"
        assert server.offer_fingerprint["legacy_create_request"] == request
        assert server.image_id == "ubuntu"
    completed.provider_response = {}
    with pytest.raises(CreateAttemptConflict):
        await env.operations.save(completed)


async def test_failed_server_visibility_uses_utc_instant_not_local_wall_clock(receipt_operations):
    env = receipt_operations
    async with env.operations._session_factory() as session:
        server = await session.scalar(select(ServerRow).where(ServerRow.id == env.server_id))
        owner = server.user_id
        server.state = "error"
        server.created_at = datetime(2026, 10, 15, 10)
        server.updated_at = datetime(2026, 10, 15, 11, 15)
        await session.commit()
    local = timezone(timedelta(hours=3, minutes=30))
    cutoff = datetime(2026, 10, 15, 14, 30, tzinfo=local)  # 11:00 UTC
    servers = SqlAlchemyServerRepository(env.operations._session_factory)
    visible, total = await servers.list_by_user_paged(
        owner, offset=0, limit=10, hide_failed_before=cutoff
    )
    assert total == 1
    assert [server.id for server in visible] == [env.server_id]
    hidden, total = await servers.list_by_user_paged(
        owner, offset=0, limit=10, hide_failed_before=cutoff + timedelta(minutes=30)
    )
    assert hidden == [] and total == 0
