"""Tests for the rebuild flow (M13-002).

Acceptance: confirmation and credential handling safe.

- Rebuild without ``confirmed=True`` is rejected BEFORE any data access.
- The command surface carries an image REFERENCE only; no password material
  exists anywhere in the flow, and the audit trail records ids/counts only.
- Ownership is enforced in the application layer (foreign server = missing).
- One ledger operation per command key; retries reuse it; completed commands
  replay without a provider call; retryable provider errors re-queue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.service import (
    RebuildActionNotAllowedError,
    RebuildCommandError,
    RebuildCommandService,
    RebuildConfirmationRequiredError,
    RebuildNotOwnerError,
    RebuildOperationFailedError,
    RebuildWorker,
    rebuild_operation_key,
)
from cloud_platform.providers.errors import ProviderConflict, ProviderUnavailable

NOW = datetime.now(UTC)
SERVER_ID = uuid4()
USER_A = uuid4()
USER_B = uuid4()


class LocalOpRepo:
    def __init__(self) -> None:
        self.ops: dict[str, Operation] = {}

    async def get_or_create(
        self,
        *,
        operation_key: str,
        operation_type: OperationType,
        resource_type: str,
        resource_id: UUID,
        provider_key: str,
    ) -> Operation:
        op = self.ops.get(operation_key)
        if op is None:
            op = Operation(
                id=uuid4(),
                operation_key=operation_key,
                operation_type=operation_type,
                resource_type=resource_type,
                resource_id=resource_id,
                provider_key=provider_key,
            )
            self.ops[operation_key] = op
        return op

    async def get(self, operation_id: UUID) -> Operation | None:
        return next((o for o in self.ops.values() if o.id == operation_id), None)

    async def get_by_key(self, operation_key: str) -> Operation | None:
        return self.ops.get(operation_key)

    async def claim(self, operation_id: UUID) -> Operation | None:
        op = await self.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Operation) -> Operation:
        return operation

    async def list_pending(self, types: Any) -> list[Operation]:
        return [o for o in self.ops.values() if o.status is OperationStatus.PENDING]


class LocalServerRepo:
    def __init__(self, server: CloudServer) -> None:
        self.servers = {server.id: server}

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def save(self, server: CloudServer) -> CloudServer:
        self.servers[server.id] = server
        return server


class FakeRegistry:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def get(self, _key: str) -> Any:
        return self._provider


class RebuildableFakeProvider:
    """Fake provider with an optional rebuild_server implementation."""

    def __init__(self, *, supports_rebuild: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = None
        if supports_rebuild:
            self.rebuild_server = self._rebuild

    async def _rebuild(self, provider_server_id: str, image_id: str) -> str:
        self.calls.append((provider_server_id, image_id))
        if self.error is not None:
            raise self.error
        return "success"


class RecordingAudit:
    """AuditRepository stand-in: records appended AuditEvent objects."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


def make_world(
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
    *,
    supports_rebuild: bool = True,
) -> tuple[
    RebuildCommandService, LocalOpRepo, LocalServerRepo, RebuildableFakeProvider, RecordingAudit
]:
    server = CloudServer(
        id=SERVER_ID,
        user_id=USER_A,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        provider_server_id=f"srv-{str(SERVER_ID)[:8]}",
        idempotency_key="ik",
        created_at=NOW,
    )
    ops = LocalOpRepo()
    servers = LocalServerRepo(server)
    provider = RebuildableFakeProvider(supports_rebuild=supports_rebuild)
    audit = RecordingAudit()
    service = RebuildCommandService(
        server_repo=servers,  # type: ignore[arg-type]
        operation_repo=ops,  # type: ignore[arg-type]
        provider_registry=FakeRegistry(provider),  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
    )
    return service, ops, servers, provider, audit


class TestConfirmationGate:
    async def test_unconfirmed_request_is_refused_before_any_access(self) -> None:
        service, ops, _servers, provider, audit = make_world()
        with pytest.raises(RebuildConfirmationRequiredError):
            await service.request(USER_A, SERVER_ID, "debian-12", "key-1", confirmed=False)
        assert audit.events == []  # nothing was looked up or recorded
        assert ops.ops == {}
        assert provider.calls == []

    async def test_confirmed_request_passes_the_gate(self) -> None:
        service, _ops, servers, provider, _audit = make_world()
        result = await service.request(USER_A, SERVER_ID, "debian-12", "key-1", confirmed=True)
        assert result.replayed is False and result.requeued is False
        assert provider.calls == [(servers.servers[SERVER_ID].provider_server_id, "debian-12")]

    async def test_missing_idempotency_key_rejected(self) -> None:
        service, _ops, _servers, _provider, _audit = make_world()
        with pytest.raises(RebuildCommandError):
            await service.request(USER_A, SERVER_ID, "debian-12", "  ", confirmed=True)


class TestCredentialSafety:
    async def test_no_credential_material_in_audit_or_operation(self) -> None:
        service, ops, _servers, _provider, audit = make_world()
        await service.request(USER_A, SERVER_ID, "debian-12", "key-1", confirmed=True)
        blob = (repr(audit.events) + repr(ops.ops)).lower()
        assert "password" not in blob
        assert "bearer" not in blob
        # the only provider-visible payload is the image reference
        assert "debian-12" in repr(audit.events)

    async def test_audit_records_actor_image_and_key(self) -> None:
        service, _ops, _servers, _provider, audit = make_world()
        await service.request(USER_A, SERVER_ID, "debian-12", "key-1", confirmed=True)
        actions = [e.action for e in audit.events]
        assert actions == ["server.rebuild_requested", "server.rebuilt"]
        rebuilt = audit.events[-1]
        assert rebuilt.actor_type is ActorType.USER
        assert rebuilt.actor_id == USER_A
        assert rebuilt.metadata["image_id"] == "debian-12"


class TestOwnershipAndStateGates:
    async def test_foreign_server_reads_as_missing(self) -> None:
        service, ops, _servers, provider, _audit = make_world()
        with pytest.raises(RebuildNotOwnerError, match="not found"):
            await service.request(USER_B, SERVER_ID, "debian-12", "k", confirmed=True)
        assert provider.calls == []
        assert ops.ops == {}

    @pytest.mark.parametrize(
        "state",
        [ServerLifecycleState.PROVISIONING, ServerLifecycleState.DELETING],
    )
    async def test_non_steady_state_refused(self, state: ServerLifecycleState) -> None:
        service, ops, _servers, provider, _audit = make_world(state=state)
        with pytest.raises(RebuildActionNotAllowedError):
            await service.request(USER_A, SERVER_ID, "debian-12", "k", confirmed=True)
        assert provider.calls == []
        assert ops.ops == {}


class TestIdempotencyAndRequeue:
    async def test_completed_command_replays_without_provider_call(self) -> None:
        service, _ops, _servers, provider, _audit = make_world()
        await service.request(USER_A, SERVER_ID, "debian-12", "k", confirmed=True)
        first_calls = len(provider.calls)
        result = await service.request(USER_A, SERVER_ID, "debian-12", "k", confirmed=True)
        assert result.replayed is True
        assert len(provider.calls) == first_calls

    async def test_retryable_error_requeues_same_key(self) -> None:
        service, ops, _servers, provider, _audit = make_world()
        provider.error = ProviderUnavailable("503")
        result = await service.request(USER_A, SERVER_ID, "debian-12", "k", confirmed=True)
        assert result.requeued is True
        op = ops.ops[rebuild_operation_key(SERVER_ID, "k")]
        assert op.status is OperationStatus.PENDING
        assert op.attempts == 1

    async def test_worker_retries_with_same_key_and_lookup(self) -> None:
        service, ops, servers, provider, _audit = make_world()
        provider.error = ProviderUnavailable("503")
        await service.request(USER_A, SERVER_ID, "debian-12", "k", confirmed=True)

        # transient error clears; a worker retries via image_lookup
        provider.error = None
        images: dict[Any, str] = {SERVER_ID: "debian-12"}
        worker = RebuildWorker(
            operation_repo=ops,  # type: ignore[arg-type]
            server_repo=servers,  # type: ignore[arg-type]
            provider_registry=FakeRegistry(provider),  # type: ignore[arg-type]
            audit_repo=RecordingAudit(),  # type: ignore[arg-type]
            image_lookup=lambda sid: images.get(sid),
        )
        counts = await worker.process_pending()
        assert counts == {"executed": 1, "requeued": 0}
        op = ops.ops[rebuild_operation_key(SERVER_ID, "k")]
        assert op.status is OperationStatus.COMPLETED
        # every attempt used the SAME target image (deterministic lookup)
        assert all(image == "debian-12" for _sid, image in provider.calls)

    async def test_permanent_error_fails_and_next_request_surfaces_it(self) -> None:
        # ProviderConflict is in the PERMANENT classification set
        service, ops, _servers, provider, _audit = make_world()
        provider.error = ProviderConflict("unacceptable image")
        with pytest.raises(RebuildOperationFailedError):
            await service.request(USER_A, SERVER_ID, "bad-image", "k", confirmed=True)
        assert ops.ops[rebuild_operation_key(SERVER_ID, "k")].status is OperationStatus.FAILED
        # replaying the same key surfaces the recorded failure, no new call
        calls_before = len(provider.calls)
        with pytest.raises(RebuildOperationFailedError):
            await service.request(USER_A, SERVER_ID, "bad-image", "k", confirmed=True)
        assert len(provider.calls) == calls_before


class TestExecutorGuards:
    async def test_provider_without_rebuild_support_fails(self) -> None:
        service, _ops, _servers, _provider, _audit = make_world(supports_rebuild=False)
        with pytest.raises(RebuildOperationFailedError, match="lacks rebuild support"):
            await service.request(USER_A, SERVER_ID, "debian-12", "k2", confirmed=True)

    async def test_state_drift_between_intent_and_execution_fails(self) -> None:
        service, ops, servers, _provider, _audit = make_world()

        drifted = Operation(
            id=uuid4(),
            operation_key=rebuild_operation_key(SERVER_ID, "drift"),
            operation_type=OperationType.SERVER_REBUILD,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        ops.ops[drifted.operation_key] = drifted
        claimed = await ops.claim(drifted.id)
        assert claimed is not None
        # the row moves to DELETING after the intent was created
        servers.servers[SERVER_ID].transition_to(ServerLifecycleState.DELETE_REQUESTED)
        with pytest.raises(RebuildOperationFailedError, match="left the expected state"):
            await service._executor.execute(
                claimed, actor_type=ActorType.USER, image_id="debian-12"
            )


class TestHetznerAdapterMapping:
    async def test_hetzner_rebuild_maps_endpoint(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        calls: list[tuple[str, str]] = []

        class Transport:
            async def request(self, method: str, path: str, **kwargs: Any):
                calls.append((method, path))
                assert method == "POST" and path.endswith("/actions/rebuild")
                assert kwargs["json"] == {"image": "debian-12"}
                from tests.unit.test_ssh_keys import _Resp

                return _Resp(201, {"action": {"status": "running"}})

        provider = HetznerCloudProvider(token="t")
        provider._client = Transport()  # type: ignore[assignment]
        status = await provider.rebuild_server("srv-1", "debian-12")
        assert status == "running"
        assert calls == [("POST", "/servers/srv-1/actions/rebuild")]
