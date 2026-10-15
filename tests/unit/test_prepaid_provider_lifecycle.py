"""Prepaid provider transitions are backed by paid coverage and real provider facts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_PREPAID_HOURLY_IRT,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.domain import Operation, OperationType
from cloud_platform.modules.operations.service import (
    DeleteOperationExecutor,
    PowerCommandService,
    PrepaidProviderLifecycleOutcome,
    PrepaidProviderLifecycleService,
    ServerStateReconciler,
    StateReconciliationOutcome,
)
from cloud_platform.providers.base import Capability, ProviderServer
from cloud_platform.providers.errors import ProviderAuthError
from cloud_platform.providers.registry import ProviderRegistry

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def server(state=ServerLifecycleState.STOPPED, *, model=BILLING_MODEL_PREPAID_HOURLY_IRT):
    return CloudServer(
        id=uuid4(),
        user_id=uuid4(),
        provider_key="test",
        provider_account_id=uuid4(),
        provider_server_id="provider-1",
        state=state,
        billing_model=model,
        prepaid_zero_since=NOW - timedelta(hours=23),
    )


class ServerRepo:
    def __init__(self, row):
        self.row = row
        self.saved = 0

    async def get(self, server_id):
        return self.row if server_id == self.row.id else None

    async def save(self, row):
        self.saved += 1
        self.row = row
        return row


class Provider:
    key = "test"
    capabilities = frozenset({Capability.COMPUTE, Capability.POWER})

    def __init__(self, status="running"):
        self.status = status
        self.power_calls = []

    async def get_server(self, provider_id):
        return ProviderServer(id=provider_id, name="test", status=self.status)

    async def power_off(self, provider_id, key):
        self.power_calls.append((provider_id, key))
        self.status = "stopped"


def dependencies(row, *, status="running", balance=0):
    repo = ServerRepo(row)
    wallet = AsyncMock()
    wallet.get.return_value = SimpleNamespace(currency="IRT", balance=balance)
    provider = Provider(status)
    registry = ProviderRegistry()
    registry.register(provider)
    power = AsyncMock()
    delete = AsyncMock()
    lifecycle = PrepaidProviderLifecycleService(
        server_repo=repo,
        wallet_repo=wallet,
        provider_registry=registry,
        power_commands=power,
        delete_commands=delete,
    )
    return lifecycle, repo, wallet, provider, power, delete, registry


class Operations:
    def __init__(self):
        self.items = {}

    async def get_by_key(self, key):
        return self.items.get(key)

    async def get_or_create(
        self, *, operation_key, operation_type, resource_type, resource_id, provider_key
    ):
        if operation_key not in self.items:
            self.items[operation_key] = Operation(
                id=uuid4(),
                operation_key=operation_key,
                operation_type=operation_type,
                resource_type=resource_type,
                resource_id=resource_id,
                provider_key=provider_key,
            )
        return self.items[operation_key]

    async def claim(self, operation_id):
        for operation in self.items.values():
            if operation.id == operation_id and operation.status.value == "pending":
                operation.mark_in_flight()
                return operation
        return None

    async def save(self, operation):
        self.items[operation.operation_key] = operation


async def test_zero_stop_uses_provider_operation_once_and_confirms_state():
    row = server(ServerLifecycleState.RUNNING)
    lifecycle, repo, _, provider, _, delete, registry = dependencies(row)
    ops = Operations()
    command = PowerCommandService(
        server_repo=repo,
        operation_repo=ops,
        provider_registry=registry,
        audit_repo=AsyncMock(),
    )
    lifecycle._power = command
    assert (
        await lifecycle.enforce(row.id, now=NOW) is PrepaidProviderLifecycleOutcome.STOP_REQUESTED
    )
    assert row.state is ServerLifecycleState.STOPPED
    assert len(provider.power_calls) == 1
    assert provider.power_calls[0][0] == row.provider_server_id
    assert await lifecycle.enforce(row.id, now=NOW) is PrepaidProviderLifecycleOutcome.STOPPED
    assert len(provider.power_calls) == 1
    assert len(ops.items) == 1
    delete.request.assert_not_awaited()


@pytest.mark.parametrize("state", [ServerLifecycleState.PROVISIONING, ServerLifecycleState.STOPPED])
async def test_provider_reconciliation_refuses_unpaid_running(state):
    row = server(state)
    row.prepaid_paid_until = NOW - timedelta(minutes=5)
    _lifecycle, repo, _, provider, _, _, registry = dependencies(row)
    audit = AsyncMock()
    reconciler = ServerStateReconciler(
        server_repo=repo, provider_registry=registry, audit_repo=audit
    )
    assert await reconciler._reconcile_server(row) is StateReconciliationOutcome.INCONCLUSIVE
    assert row.state is state
    assert repo.saved == 0
    assert provider.status == "running"


async def test_capture_must_commit_paid_coverage_before_running():
    row = server(ServerLifecycleState.PROVISIONING)
    row.prepaid_zero_since = None
    _lifecycle, repo, _, _, _, _, registry = dependencies(row)
    audit = AsyncMock()

    async def capture(server_id):
        assert server_id == row.id
        row.prepaid_paid_until = datetime.now(UTC) + timedelta(hours=1)

    reconciler = ServerStateReconciler(
        server_repo=repo,
        provider_registry=registry,
        audit_repo=audit,
        prepaid_capture=capture,
    )
    assert await reconciler._reconcile_server(row) is StateReconciliationOutcome.REPAIRED
    assert row.state is ServerLifecycleState.RUNNING


async def test_zero_stop_is_owner_scoped_and_stop_failure_does_not_forge_state():
    row = server(ServerLifecycleState.RUNNING)
    lifecycle, repo, _, provider, power, delete, _ = dependencies(row)
    power.power_off.side_effect = ProviderAuthError("denied")
    with pytest.raises(ProviderAuthError):
        await lifecycle.enforce(row.id, now=NOW)
    power.power_off.assert_awaited_once_with(
        row.user_id, row.id, f"prepaid-zero:{row.prepaid_zero_since.isoformat()}"
    )
    assert row.state is ServerLifecycleState.RUNNING
    assert repo.saved == 0
    delete.request.assert_not_awaited()
    assert provider.power_calls == []


async def test_positive_wallet_unpaid_stop_never_deletes():
    row = server(ServerLifecycleState.RUNNING)
    row.prepaid_zero_since = None
    lifecycle, _, _, _, power, delete, _ = dependencies(row, balance=500_000)
    assert (
        await lifecycle.enforce_unpaid(row.id, now=NOW)
        is PrepaidProviderLifecycleOutcome.STOP_REQUESTED
    )
    power.power_off.assert_awaited_once()
    delete.request.assert_not_awaited()


async def test_delete_only_after_24_hours_at_zero_with_provider_stopped():
    row = server()
    lifecycle, _, wallet, provider, power, delete, _ = dependencies(row, status="stopped")
    assert await lifecycle.enforce(row.id, now=NOW) is PrepaidProviderLifecycleOutcome.STOPPED
    delete.request.assert_not_awaited()
    row.prepaid_zero_since = NOW - timedelta(hours=24)
    assert (
        await lifecycle.enforce(row.id, now=NOW) is PrepaidProviderLifecycleOutcome.DELETE_REQUESTED
    )
    assert (
        await lifecycle.enforce(row.id, now=NOW) is PrepaidProviderLifecycleOutcome.DELETE_REQUESTED
    )
    assert delete.request.await_args_list[0] == delete.request.await_args_list[1]
    delete.request.assert_awaited_with(
        row.user_id,
        row.id,
        f"prepaid-zero:{row.prepaid_zero_since.isoformat()}",
        execute_inline=True,
    )
    assert wallet.get.await_count >= 5
    assert provider.power_calls == []
    power.power_off.assert_not_awaited()


async def test_recharge_or_provider_resurrection_cannot_trigger_delete():
    row = server()
    row.prepaid_zero_since = NOW - timedelta(hours=25)
    lifecycle, _, wallet, provider, power, delete, _ = dependencies(row, status="stopped")
    wallet.get.side_effect = [
        SimpleNamespace(currency="IRT", balance=0),
        SimpleNamespace(currency="IRT", balance=500),
    ]
    assert await lifecycle.enforce(row.id, now=NOW) is PrepaidProviderLifecycleOutcome.SKIPPED
    delete.request.assert_not_awaited()
    wallet.get.side_effect = None
    wallet.get.return_value = SimpleNamespace(currency="IRT", balance=0)
    provider.status = "running"
    assert await lifecycle.enforce(row.id, now=NOW) is PrepaidProviderLifecycleOutcome.INCONCLUSIVE
    power.power_off.assert_not_awaited()
    delete.request.assert_not_awaited()
    assert provider.power_calls == []


@pytest.mark.parametrize(
    "model, expected_calls",
    [
        (BILLING_MODEL_PREPAID_HOURLY_IRT, 0),
        (BILLING_MODEL_HOURLY, 1),
    ],
)
async def test_delete_skips_only_prepaid_retroactive_final_charge(model, expected_calls):
    row = server(ServerLifecycleState.DELETE_REQUESTED, model=model)
    repo = ServerRepo(row)
    operation = Operation(
        id=uuid4(),
        operation_key=f"delete:{row.id}",
        operation_type=OperationType.SERVER_DELETE,
        resource_type="server",
        resource_id=row.id,
        provider_key=row.provider_key,
    )
    operation.mark_in_flight()
    provider = Provider("stopped")
    registry = ProviderRegistry()
    registry.register(provider)
    final_charge = AsyncMock()
    final_charge.charge_final.return_value = SimpleNamespace(charged_minor=5, capped=False)
    executor = DeleteOperationExecutor(
        operation_repo=AsyncMock(),
        server_repo=repo,
        provider_registry=registry,
        final_charge=final_charge,
        hold_repo=AsyncMock(),
        hold_service=AsyncMock(),
        wallet_repo=AsyncMock(),
        audit_repo=AsyncMock(),
    )
    executor._ensure_provider_absent = AsyncMock(return_value=None)
    await executor._execute_in_span(operation, actor_type=ActorType.SYSTEM)
    assert final_charge.charge_final.await_count == expected_calls
    assert row.state is ServerLifecycleState.DELETED
    assert operation.provider_response["charged_minor"] == (5 if expected_calls else 0)
