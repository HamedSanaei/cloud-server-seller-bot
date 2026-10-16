"""Queued administrator commands preserve attribution and cannot outlive revocation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.operations.domain import Operation, OperationStatus, OperationType
from cloud_platform.modules.operations.service import PowerCommandError, PowerOperationExecutor
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.providers.base import Capability
from cloud_platform.providers.registry import ProviderRegistry


def environment():
    admin = uuid4()
    server = CloudServer(
        id=uuid4(),
        user_id=uuid4(),
        provider_key="fixture",
        provider_account_id=uuid4(),
        provider_server_id="fixture-provider-id",
        state=ServerLifecycleState.RUNNING,
    )
    operation = Operation(
        id=uuid4(),
        operation_key="admin-support:fixture",
        operation_type=OperationType.POWER_OFF,
        resource_type="server",
        resource_id=server.id,
        provider_key="fixture",
        status=OperationStatus.IN_FLIGHT,
        attempts=1,
        provider_response={"admin_support": {"actor_id": str(admin), "reason": "Customer request"}},
    )
    repo = SimpleNamespace(save=AsyncMock(), authorize_admin_support=AsyncMock(return_value=admin))
    provider = SimpleNamespace(
        key="fixture", capabilities=frozenset({Capability.POWER}), power_off=AsyncMock()
    )
    registry = ProviderRegistry()
    registry.register(provider)
    audit = SimpleNamespace(append=AsyncMock())
    executor = PowerOperationExecutor(
        operation_repo=repo,
        server_repo=SimpleNamespace(get=AsyncMock(return_value=server), save=AsyncMock()),
        provider_registry=registry,
        audit_repo=audit,
    )
    return admin, server, operation, repo, provider, audit, executor


async def test_worker_restores_real_administrator_attribution_and_preserves_intent():
    admin, server, operation, repo, provider, audit, executor = environment()
    await executor.execute(operation, actor_type=ActorType.SYSTEM)
    provider.power_off.assert_awaited_once()
    assert repo.authorize_admin_support.await_count == 2
    assert operation.provider_response["admin_support"]["actor_id"] == str(admin)
    assert (
        operation.status is OperationStatus.COMPLETED
        and server.state is ServerLifecycleState.STOPPED
    )
    event = audit.append.await_args.args[0]
    assert event.actor_type is ActorType.ADMIN and event.actor_id == admin


@pytest.mark.parametrize("after_read", [False, True])
async def test_revoked_actor_never_reaches_provider_mutation(after_read):
    admin, _, operation, repo, provider, _, executor = environment()
    repo.authorize_admin_support.side_effect = (
        [admin, PermissionDeniedError("revoked")]
        if after_read
        else PermissionDeniedError("revoked")
    )
    with pytest.raises(PowerCommandError):
        await executor.execute(operation, actor_type=ActorType.SYSTEM)
    provider.power_off.assert_not_awaited()
    assert operation.status is OperationStatus.FAILED
