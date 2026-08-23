"""Tests for power commands (M07-006).

Acceptance: ownership/capability/idempotency enforced.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ProvisioningSpec,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.service import (
    NotServerOwnerError,
    PowerAction,
    PowerActionNotAllowedError,
    PowerCommandError,
    PowerCommandService,
    PowerOperationFailedError,
    PowerOperationInProgressError,
    ProvisioningWorker,
    power_operation_key,
)
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    ProviderImage,
    ProviderServer,
)
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderUnavailable,
)
from cloud_platform.providers.registry import ProviderRegistry

USER_ID = uuid4()
OTHER_USER_ID = uuid4()
SERVER_ID = uuid4()
SPEC = ProvisioningSpec(plan_id="cx22", location_id="fsn1", currency="EUR")


class FakeOpRepo:
    def __init__(self) -> None:
        self.by_key: dict[str, Operation] = {}
        self.saved: list[Operation] = []

    def add(self, op: Operation) -> None:
        self.by_key[op.operation_key] = op

    async def get_or_create(
        self,
        *,
        operation_key: str,
        operation_type: OperationType,
        resource_type: str,
        resource_id: UUID,
        provider_key: str,
    ) -> Operation:
        op = self.by_key.get(operation_key)
        if op is None:
            op = Operation(
                id=uuid4(),
                operation_key=operation_key,
                operation_type=operation_type,
                resource_type=resource_type,
                resource_id=resource_id,
                provider_key=provider_key,
            )
            self.by_key[operation_key] = op
        return op

    async def get(self, operation_id: UUID) -> Operation | None:
        for op in self.by_key.values():
            if op.id == operation_id:
                return op
        return None

    async def get_by_key(self, operation_key: str) -> Operation | None:
        return self.by_key.get(operation_key)

    async def list_in_flight(self, operation_type: OperationType) -> list[Operation]:
        return [
            op
            for op in self.by_key.values()
            if op.operation_type is operation_type and op.status is OperationStatus.IN_FLIGHT
        ]

    async def list_pending(self, operation_types: Any) -> list[Operation]:
        return [
            op
            for op in self.by_key.values()
            if op.operation_type in operation_types and op.status is OperationStatus.PENDING
        ]

    async def claim(self, operation_id: UUID) -> Operation | None:
        op = await self.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Operation) -> Operation:
        self.saved.append(operation)
        return operation


class FakeServerRepo:
    def __init__(self, servers: list[CloudServer]) -> None:
        self.servers = {s.id: s for s in servers}
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_requested(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.REQUESTED]

    async def list_provisioning(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.PROVISIONING]

    async def list_running(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.RUNNING]

    async def list_stopped(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.STOPPED]

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        return SPEC

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class FakeProvider:
    key = "hetzner"
    capabilities = frozenset({Capability.COMPUTE, Capability.POWER})

    def __init__(self, power_error: Exception | None = None) -> None:
        self.power_error = power_error
        self.calls: list[tuple[str, str]] = []  # (method, idempotency key)

    async def list_images(self) -> list[ProviderImage]:
        return [ProviderImage(id="img-linux", name="debian", os_family="linux", architecture="x86")]

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        return ProviderServer(id=provider_server_id, name="srv-x", status="running")

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: Any
    ) -> ProviderServer:
        raise AssertionError("create_server must not be called by power commands")

    async def power_on(self, provider_server_id: str, idempotency_key: Any) -> None:
        self.calls.append(("power_on", idempotency_key.value))
        if self.power_error is not None:
            raise self.power_error

    async def power_off(self, provider_server_id: str, idempotency_key: Any) -> None:
        self.calls.append(("power_off", idempotency_key.value))
        if self.power_error is not None:
            raise self.power_error

    async def reboot(self, provider_server_id: str, idempotency_key: Any) -> None:
        self.calls.append(("reboot", idempotency_key.value))
        if self.power_error is not None:
            raise self.power_error


def _server(
    state: ServerLifecycleState = ServerLifecycleState.STOPPED,
    user_id: UUID = USER_ID,
    server_id: UUID = SERVER_ID,
    provider_server_id: str | None = "prov-1",
) -> CloudServer:
    return CloudServer(
        id=server_id,
        user_id=user_id,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        idempotency_key="cmd-1",
        provider_server_id=provider_server_id,
    )


class _Deps:
    def __init__(
        self,
        server: CloudServer,
        provider: FakeProvider | None = None,
        register: bool = True,
    ) -> None:
        self.server = server
        self.ops = FakeOpRepo()
        self.server_repo = FakeServerRepo([server])
        self.provider = provider if provider is not None else FakeProvider()
        self.registry = ProviderRegistry()
        if register:
            self.registry.register(self.provider)  # type: ignore[arg-type]
        self.wallets = AsyncMock()
        self.holds = AsyncMock()
        self.audit = AsyncMock()
        self.audit.append = AsyncMock(side_effect=lambda e: e)
        self.service = PowerCommandService(
            server_repo=self.server_repo,  # type: ignore[arg-type]
            operation_repo=self.ops,  # type: ignore[arg-type]
            provider_registry=self.registry,
            audit_repo=self.audit,  # type: ignore[arg-type]
        )
        self.worker = ProvisioningWorker(
            operation_repo=self.ops,  # type: ignore[arg-type]
            server_repo=self.server_repo,  # type: ignore[arg-type]
            provider_registry=self.registry,
            image_selector=object(),  # type: ignore[arg-type]
            wallet_repo=self.wallets,  # type: ignore[arg-type]
            hold_repo=self.holds,  # type: ignore[arg-type]
            audit_repo=self.audit,  # type: ignore[arg-type]
        )

    def audit_events(self) -> list:
        return [c.args[0] for c in self.audit.append.call_args_list]


class TestOwnership:
    async def test_other_users_server_is_indistinguishable_from_missing(self) -> None:
        server = _server(user_id=OTHER_USER_ID)
        deps = _Deps(server)

        with pytest.raises(NotServerOwnerError):
            await deps.service.power_on(USER_ID, SERVER_ID, "key-1")
        with pytest.raises(NotServerOwnerError):
            await deps.service.power_off(USER_ID, uuid4(), "key-1")

        assert deps.ops.by_key == {}
        assert deps.provider.calls == []

    async def test_missing_server(self) -> None:
        server = _server()
        deps = _Deps(server)

        with pytest.raises(NotServerOwnerError):
            await deps.service.reboot(USER_ID, uuid4(), "key-1")
        assert deps.provider.calls == []


class TestCapabilityGate:
    @pytest.mark.parametrize(
        ("action", "state", "method"),
        [
            ("power_off", ServerLifecycleState.STOPPED, "power_off"),
            ("power_on", ServerLifecycleState.RUNNING, "power_on"),
            ("reboot", ServerLifecycleState.STOPPED, "reboot"),
            ("power_on", ServerLifecycleState.PROVISIONING, "power_on"),
            ("power_off", ServerLifecycleState.ERROR, "power_off"),
            ("reboot", ServerLifecycleState.MANUAL_REVIEW, "reboot"),
        ],
    )
    async def test_wrong_state_rejected(
        self, action: str, state: ServerLifecycleState, method: str
    ) -> None:
        server = _server(state=state)
        deps = _Deps(server)

        with pytest.raises(PowerActionNotAllowedError):
            await getattr(deps.service, method)(USER_ID, SERVER_ID, "key-1")
        assert deps.provider.calls == []
        assert deps.ops.by_key == {}

    async def test_provider_without_power_capability_rejected(self) -> None:
        server = _server(state=ServerLifecycleState.STOPPED)
        provider = FakeProvider()
        provider.capabilities = frozenset({Capability.COMPUTE})
        deps = _Deps(server, provider)

        with pytest.raises(PowerOperationFailedError, match="POWER capability"):
            await deps.service.power_on(USER_ID, SERVER_ID, "key-1")
        assert deps.provider.calls == []
        op_key = power_operation_key(PowerAction.POWER_ON, SERVER_ID, "key-1")
        assert deps.ops.by_key[op_key].status is OperationStatus.FAILED

    async def test_unknown_provider_rejected(self) -> None:
        server = _server(state=ServerLifecycleState.STOPPED)
        deps = _Deps(server, register=False)

        with pytest.raises(PowerOperationFailedError, match="unknown provider"):
            await deps.service.power_on(USER_ID, SERVER_ID, "key-1")

    async def test_no_provider_resource_id_rejected(self) -> None:
        server = _server(state=ServerLifecycleState.STOPPED, provider_server_id=None)
        deps = _Deps(server)

        with pytest.raises(PowerOperationFailedError, match="no provider resource id"):
            await deps.service.power_on(USER_ID, SERVER_ID, "key-1")

    async def test_empty_idempotency_key_rejected(self) -> None:
        server = _server(state=ServerLifecycleState.STOPPED)
        deps = _Deps(server)

        with pytest.raises(PowerCommandError):
            await deps.service.power_on(USER_ID, SERVER_ID, "   ")
        assert deps.provider.calls == []


class TestIdempotency:
    async def test_power_on_executes_once_and_updates_state(self) -> None:
        server = _server(state=ServerLifecycleState.STOPPED)
        deps = _Deps(server)

        result = await deps.service.power_on(USER_ID, SERVER_ID, "key-1")

        assert result.replayed is False
        assert result.requeued is False
        op_key = power_operation_key(PowerAction.POWER_ON, SERVER_ID, "key-1")
        assert deps.provider.calls == [("power_on", op_key)]
        op = deps.ops.by_key[op_key]
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response == {"action": "power_on", "idempotency_key": op_key}
        assert server.state is ServerLifecycleState.RUNNING
        event = deps.audit_events()[0]
        assert event.action == "server.powered_on"
        assert event.actor_type.value == "user"
        assert event.actor_id == USER_ID

    async def test_replay_same_key_no_provider_call(self) -> None:
        server = _server(state=ServerLifecycleState.STOPPED)
        deps = _Deps(server)

        first = await deps.service.power_on(USER_ID, SERVER_ID, "key-1")
        second = await deps.service.power_on(USER_ID, SERVER_ID, "key-1")

        assert first.replayed is False
        assert second.replayed is True
        assert len(deps.provider.calls) == 1
        # the op is COMPLETED, so the second call returns before any claim
        assert len(deps.ops.by_key) == 1

    async def test_different_key_is_a_new_intent(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps(server)

        await deps.service.reboot(USER_ID, SERVER_ID, "key-a")
        await deps.service.reboot(USER_ID, SERVER_ID, "key-b")

        assert len(deps.provider.calls) == 2
        assert {k for _, k in deps.provider.calls} == {
            power_operation_key(PowerAction.REBOOT, SERVER_ID, "key-a"),
            power_operation_key(PowerAction.REBOOT, SERVER_ID, "key-b"),
        }

    async def test_in_flight_operation_is_rejected(self) -> None:
        server = _server(state=ServerLifecycleState.STOPPED)
        deps = _Deps(server)
        op = await deps.ops.get_or_create(
            operation_key=power_operation_key(PowerAction.POWER_ON, SERVER_ID, "key-1"),
            operation_type=OperationType.POWER_ON,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        op.mark_in_flight()

        with pytest.raises(PowerOperationInProgressError):
            await deps.service.power_on(USER_ID, SERVER_ID, "key-1")
        assert deps.provider.calls == []

    async def test_retryable_error_requeues(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        provider = FakeProvider(power_error=ProviderUnavailable("503"))
        deps = _Deps(server, provider)

        result = await deps.service.power_off(USER_ID, SERVER_ID, "key-1")

        assert result.replayed is False
        assert result.requeued is True
        op = deps.ops.by_key[power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1")]
        assert op.status is OperationStatus.PENDING
        assert op.error == "503"
        assert server.state is ServerLifecycleState.RUNNING  # unchanged
        assert deps.audit_events()[0].action == "server.power_requeued"

    async def test_permanent_error_fails(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        provider = FakeProvider(power_error=ProviderAuthError("401"))
        deps = _Deps(server, provider)

        with pytest.raises(PowerOperationFailedError, match="401"):
            await deps.service.power_off(USER_ID, SERVER_ID, "key-1")
        op = deps.ops.by_key[power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1")]
        assert op.status is OperationStatus.FAILED
        assert server.state is ServerLifecycleState.RUNNING
        assert deps.audit_events()[0].action == "server.power_failed"

    async def test_replay_of_failed_operation_surfaces_error(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps(server)
        op = await deps.ops.get_or_create(
            operation_key=power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1"),
            operation_type=OperationType.POWER_OFF,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        op.mark_in_flight()
        op.fail("boom")

        with pytest.raises(PowerOperationFailedError, match="boom"):
            await deps.service.power_off(USER_ID, SERVER_ID, "key-1")
        assert deps.provider.calls == []

    async def test_power_off_updates_state(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps(server)

        result = await deps.service.power_off(USER_ID, SERVER_ID, "key-1")

        assert result.replayed is False
        assert server.state is ServerLifecycleState.STOPPED
        assert deps.audit_events()[0].action == "server.powered_off"

    async def test_reboot_keeps_running(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps(server)

        result = await deps.service.reboot(USER_ID, SERVER_ID, "key-1")

        assert result.replayed is False
        assert server.state is ServerLifecycleState.RUNNING  # unchanged
        assert deps.provider.calls == [
            ("reboot", power_operation_key(PowerAction.REBOOT, SERVER_ID, "key-1"))
        ]
        assert deps.audit_events()[0].action == "server.rebooted"


class TestWorkerPowerQueue:
    async def test_executes_pending_power_operation_as_system(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps(server)
        op = await deps.ops.get_or_create(
            operation_key=power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1"),
            operation_type=OperationType.POWER_OFF,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        assert op.status is OperationStatus.PENDING

        counts = await deps.worker.process_pending_power(limit=5)

        assert counts == {"executed": 1, "requeued": 0, "failed": 0, "contended": 0}
        assert server.state is ServerLifecycleState.STOPPED
        assert deps.provider.calls == [
            ("power_off", power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1"))
        ]
        event = deps.audit_events()[0]
        assert event.action == "server.powered_off"
        assert event.actor_type.value == "system"
        assert event.actor_id is None

    async def test_retryable_error_counts_requeued(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        provider = FakeProvider(power_error=ProviderUnavailable("503"))
        deps = _Deps(server, provider)
        await deps.ops.get_or_create(
            operation_key=power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1"),
            operation_type=OperationType.POWER_OFF,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )

        counts = await deps.worker.process_pending_power(limit=5)

        assert counts["requeued"] == 1
        assert server.state is ServerLifecycleState.RUNNING

    async def test_permanent_error_counts_failed(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        provider = FakeProvider(power_error=ProviderAuthError("401"))
        deps = _Deps(server, provider)
        await deps.ops.get_or_create(
            operation_key=power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1"),
            operation_type=OperationType.POWER_OFF,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )

        counts = await deps.worker.process_pending_power(limit=5)

        assert counts["failed"] == 1

    async def test_claim_race_counts_contended(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps(server)
        await deps.ops.get_or_create(
            operation_key=power_operation_key(PowerAction.POWER_OFF, SERVER_ID, "key-1"),
            operation_type=OperationType.POWER_OFF,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        deps.ops.claim = AsyncMock(return_value=None)  # type: ignore[method-assign]

        counts = await deps.worker.process_pending_power(limit=5)

        assert counts["contended"] == 1
        assert deps.provider.calls == []

    async def test_create_operations_are_not_in_power_queue(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps(server)
        await deps.ops.get_or_create(
            operation_key=f"server-create:{SERVER_ID}",
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )

        counts = await deps.worker.process_pending_power(limit=5)

        assert counts == {"executed": 0, "requeued": 0, "failed": 0, "contended": 0}
        assert deps.provider.calls == []
