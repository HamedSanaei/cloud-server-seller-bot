"""Tests for the delete-server saga (M07-007).

Acceptance: delete request -> provider absent -> billing final -> deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.billing.service import FinalChargeResult, MissingSnapshotError
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.service import (
    DeleteActionNotAllowedError,
    DeleteCommandService,
    DeleteNotOwnerError,
    DeleteOperationExecutor,
    DeleteOperationFailedError,
    DeleteOperationInProgressError,
    DeleteWorker,
    delete_operation_key,
)
from cloud_platform.modules.wallet.domain import Hold, HoldStatus, Wallet
from cloud_platform.providers.base import ProviderServer
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderNotFound,
    ProviderUnavailable,
)
from cloud_platform.providers.waiter import WaitOutcome, WaitResult

T0 = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
SERVER_ID = uuid4()
USER_ID = uuid4()
OTHER_USER = uuid4()
WALLET_ID = uuid4()
IK = "del-1"
HOLD_KEY = "server-create:order-1"
OP_KEY = delete_operation_key(SERVER_ID, IK)


def _server(
    state: ServerLifecycleState = ServerLifecycleState.RUNNING,
    **kw: object,
) -> CloudServer:
    defaults: dict[str, object] = dict(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        idempotency_key="order-1",
        created_at=T0,
        provider_server_id="prov-1",
        quantum_seconds=3600,
    )
    defaults.update(kw)
    return CloudServer(**defaults)  # type: ignore[arg-type]


class _FakeProvider:
    def __init__(
        self, absent_after_delete: bool = True, delete_error: Exception | None = None
    ) -> None:
        self.absent = not absent_after_delete
        self.delete_error = delete_error
        self.delete_calls: list[str] = []
        self.get_calls = 0

    async def delete_server(self, provider_server_id: str, idempotency_key) -> None:
        self.delete_calls.append(provider_server_id)
        if self.delete_error is not None:
            raise self.delete_error

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        self.get_calls += 1
        if self.absent:
            return None
        return ProviderServer(id=provider_server_id, name="srv", status="running")


class _FakeWaiter:
    def __init__(self, outcome: WaitOutcome = WaitOutcome.COMPLETED, polls: int = 1) -> None:
        self.outcome = outcome
        self.polls = polls
        self.probe = None

    async def wait_for(self, probe):
        self.probe = probe
        return WaitResult(
            self.outcome,
            self.polls,
            0.1,
            "detail" if self.outcome is not WaitOutcome.COMPLETED else None,
        )


class _FakeFinalCharge:
    def __init__(
        self, charged: int = 1000, capped: bool = False, raise_missing: bool = False
    ) -> None:
        self.charged = charged
        self.capped = capped
        self.raise_missing = raise_missing
        self.calls: list[tuple[CloudServer, datetime]] = []

    async def charge_final(self, server: CloudServer, deleted_at: datetime) -> FinalChargeResult:
        self.calls.append((server, deleted_at))
        if self.raise_missing:
            raise MissingSnapshotError(f"server {server.id} has no price snapshot")
        return FinalChargeResult(
            charged_minor=self.charged,
            captured_hold=False,
            posted_entry_key="final:x",
            replayed=False,
            capped=self.capped,
        )


class Fakes:
    def __init__(
        self,
        server: CloudServer | None = None,
        provider: _FakeProvider | None = None,
        final: _FakeFinalCharge | None = None,
        waiter: _FakeWaiter | None = None,
        holds: dict[str, Hold] | None = None,
    ) -> None:
        self.servers: dict[UUID, CloudServer] = {(server or _server()).id: server or _server()}
        self.provider = provider or _FakeProvider()
        self.final = final or _FakeFinalCharge()
        self.waiter = waiter or _FakeWaiter()
        self.holds = dict(holds or {})
        self.released: list[str] = []
        self.ops: dict[str, Operation] = {}
        self.audit = AsyncMock()

    # -- server repo ------------------------------------------------------
    async def server_get(self, server_id) -> CloudServer | None:
        return self.servers.get(server_id)

    async def server_save(self, server: CloudServer) -> CloudServer:
        self.servers[server.id] = server
        return server

    # -- operation repo -----------------------------------------------------
    async def ops_get_or_create(
        self, *, operation_key, operation_type, resource_type, resource_id, provider_key
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

    async def ops_get_by_key(self, key: str) -> Operation | None:
        return self.ops.get(key)

    async def ops_claim(self, operation_id: UUID) -> Operation | None:
        for op in self.ops.values():
            if op.id == operation_id:
                if op.status is OperationStatus.PENDING:
                    op.mark_in_flight()
                    return op
                return None
        return None

    async def ops_save(self, operation: Operation) -> Operation:
        self.ops[operation.operation_key] = operation
        return operation

    async def ops_list_pending(self, types) -> list[Operation]:
        return [
            op
            for op in self.ops.values()
            if op.status is OperationStatus.PENDING and op.operation_type in types
        ]

    # -- wallet / hold ------------------------------------------------------
    async def wallet_get(self, user_id) -> Wallet | None:
        return Wallet(USER_ID, id=WALLET_ID, balance=10_000)

    async def hold_get(self, wallet_id, key: str) -> Hold | None:
        return self.holds.get(key)

    async def hold_release(self, wallet_id, hold_id, key: str) -> Hold:
        hold = self.holds[key]
        hold.release()
        self.released.append(key)
        return hold

    def make_service(self) -> DeleteCommandService:
        h = self

        @dataclass
        class _ServerRepo:
            async def get(self, server_id):
                return await h.server_get(server_id)

            async def save(self, server):
                return await h.server_save(server)

        @dataclass
        class _OpRepo:
            async def get_or_create(self, **kw):
                return await h.ops_get_or_create(**kw)

            async def get_by_key(self, key):
                return await h.ops_get_by_key(key)

            async def claim(self, operation_id):
                return await h.ops_claim(operation_id)

            async def save(self, operation):
                return await h.ops_save(operation)

            async def list_pending(self, types):
                return await h.ops_list_pending(types)

        @dataclass
        class _HoldRepo:
            async def get_by_idempotency(self, wallet_id, key):
                return await h.hold_get(wallet_id, key)

        @dataclass
        class _HoldService:
            async def release_hold(self, wallet_id, hold_id, key):
                return await h.hold_release(wallet_id, hold_id, key)

        @dataclass
        class _WalletRepo:
            async def get(self, user_id):
                return await h.wallet_get(user_id)

        class _Registry:
            async def _get(self, key):
                return h.provider

            def get(self, key):
                if key != "hetzner":
                    raise KeyError(key)
                return h.provider

        executor = DeleteOperationExecutor(
            operation_repo=_OpRepo(),
            server_repo=_ServerRepo(),
            provider_registry=_Registry(),
            final_charge=h.final,
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            wallet_repo=_WalletRepo(),
            audit_repo=h.audit,
            waiter=h.waiter,
        )
        return DeleteCommandService(
            server_repo=_ServerRepo(),
            operation_repo=_OpRepo(),
            provider_registry=_Registry(),
            final_charge=h.final,
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            wallet_repo=_WalletRepo(),
            audit_repo=h.audit,
            executor=executor,
        )

    def make_worker(self, service: DeleteCommandService) -> DeleteWorker:
        return DeleteWorker(
            operation_repo=service._ops,
            executor=service._executor,
        )


def _hold(status: HoldStatus = HoldStatus.CREATED) -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=1000,
        currency="EUR",
        idempotency_key=HOLD_KEY,
        id=uuid4(),
        status=status,
    )


class TestDeleteRequest:
    async def test_happy_path(self) -> None:
        server = _server()
        fakes = Fakes(server=server)
        result = await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        assert result.server.state is ServerLifecycleState.DELETED
        assert result.server.deleted_at is not None
        assert result.replayed is False
        assert result.requeued is False
        # provider absent exactly once per operation key
        assert fakes.provider.delete_calls == ["prov-1"]
        # billing final ran with the deletion timestamp
        assert len(fakes.final.calls) == 1
        charged_server, deleted_at = fakes.final.calls[0]
        assert charged_server.id == SERVER_ID
        assert charged_server.state is ServerLifecycleState.DELETED
        assert deleted_at == result.server.deleted_at
        # operation completed with the correlation
        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response["charged_minor"] == 1000
        assert op.provider_response["provider_server_id"] == "prov-1"
        # the audit trail
        actions = [c.args[0].action for c in fakes.audit.append.call_args_list]
        assert "server.delete_requested" in actions
        assert "server.deleted" in actions

    async def test_replay_of_completed_command(self) -> None:
        server = _server()
        fakes = Fakes(server=server)
        service = fakes.make_service()
        first = await service.request(USER_ID, SERVER_ID, IK)
        assert first.replayed is False
        deletes_before = len(fakes.provider.delete_calls)

        server_after = _server(state=ServerLifecycleState.DELETED)
        server_after.id = SERVER_ID
        server_after.user_id = USER_ID
        server_after.provider_key = "hetzner"
        server_after.provider_server_id = "prov-1"
        server_after.deleted_at = first.server.deleted_at
        fakes.servers[SERVER_ID] = server_after

        second = await service.request(USER_ID, SERVER_ID, IK)

        assert second.replayed is True
        assert len(fakes.provider.delete_calls) == deletes_before  # no provider call
        assert len(fakes.final.calls) == 1  # no second charge

    async def test_not_owner_rejected(self) -> None:
        server = _server()
        fakes = Fakes(server=server)
        with pytest.raises(DeleteNotOwnerError):
            await fakes.make_service().request(OTHER_USER, SERVER_ID, IK)
        assert fakes.ops == {}
        assert server.state is ServerLifecycleState.RUNNING

    async def test_unknown_server_indistinguishable(self) -> None:
        fakes = Fakes()
        with pytest.raises(DeleteNotOwnerError):
            await fakes.make_service().request(USER_ID, uuid4(), IK)

    async def test_state_gate(self) -> None:
        server = _server(state=ServerLifecycleState.PROVISIONING)
        fakes = Fakes(server=server)
        with pytest.raises(DeleteActionNotAllowedError):
            await fakes.make_service().request(USER_ID, SERVER_ID, IK)
        assert fakes.ops == {}
        assert server.state is ServerLifecycleState.PROVISIONING

    async def test_in_flight_rejected(self) -> None:
        server = _server(state=ServerLifecycleState.DELETE_REQUESTED)
        fakes = Fakes(server=server)
        op = Operation(
            id=uuid4(),
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_DELETE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
            status=OperationStatus.IN_FLIGHT,
        )
        fakes.ops[OP_KEY] = op
        with pytest.raises(DeleteOperationInProgressError):
            await fakes.make_service().request(USER_ID, SERVER_ID, IK)

    async def test_failed_op_surfaces_recorded_error(self) -> None:
        server = _server(state=ServerLifecycleState.DELETE_REQUESTED)
        fakes = Fakes(server=server)
        op = Operation(
            id=uuid4(),
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_DELETE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
            status=OperationStatus.FAILED,
            error="boom",
        )
        fakes.ops[OP_KEY] = op
        with pytest.raises(DeleteOperationFailedError, match="boom"):
            await fakes.make_service().request(USER_ID, SERVER_ID, IK)

    async def test_idempotency_key_required(self) -> None:
        fakes = Fakes()
        with pytest.raises(Exception, match="idempotency_key"):
            await fakes.make_service().request(USER_ID, SERVER_ID, "  ")


class TestSagaSteps:
    async def test_provider_not_found_on_delete_is_success(self) -> None:
        server = _server()
        fakes = Fakes(server=server, provider=_FakeProvider(delete_error=ProviderNotFound("404")))
        result = await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        assert result.server.state is ServerLifecycleState.DELETED
        assert result.requeued is False
        assert len(fakes.final.calls) == 1

    async def test_retryable_provider_error_requeues(self) -> None:
        server = _server()
        fakes = Fakes(
            server=server, provider=_FakeProvider(delete_error=ProviderUnavailable("down"))
        )
        result = await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        assert result.requeued is True
        # the server is mid-deletion and the operation waits for a retry
        assert result.server.state is ServerLifecycleState.DELETING
        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.PENDING
        assert op.attempts == 1
        assert op.error == "down"
        assert fakes.final.calls == []  # no billing before confirmed absence
        actions = [c.args[0].action for c in fakes.audit.append.call_args_list]
        assert "server.delete_requeued" in actions

    async def test_permanent_provider_error_fails(self) -> None:
        server = _server()
        fakes = Fakes(
            server=server,
            provider=_FakeProvider(delete_error=ProviderAuthError("bad token")),
        )
        with pytest.raises(DeleteOperationFailedError, match="bad token"):
            await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.FAILED
        assert fakes.servers[SERVER_ID].state is ServerLifecycleState.DELETING
        assert fakes.final.calls == []
        actions = [c.args[0].action for c in fakes.audit.append.call_args_list]
        assert "server.delete_failed" in actions

    async def test_absence_wait_timeout_requeues(self) -> None:
        server = _server()
        provider = _FakeProvider(absent_after_delete=False)  # still visible
        fakes = Fakes(
            server=server,
            provider=provider,
            waiter=_FakeWaiter(outcome=WaitOutcome.TIMEOUT, polls=3),
        )
        result = await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        assert result.requeued is True
        assert result.server.state is ServerLifecycleState.DELETING
        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.PENDING
        assert "re-verifying" in (op.error or "")
        assert fakes.final.calls == []  # never bill before absence

    async def test_no_provider_resource_releases_hold_and_skips_billing(self) -> None:
        server = _server(state=ServerLifecycleState.ERROR, provider_server_id=None)
        fakes = Fakes(server=server, holds={HOLD_KEY: _hold()})
        result = await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        assert result.server.state is ServerLifecycleState.DELETED
        assert fakes.provider.delete_calls == []  # nothing to delete
        assert fakes.final.calls == []  # no usage -> no charge
        assert fakes.released == [HOLD_KEY]  # reserved funds returned
        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response["charged_minor"] == 0

    async def test_missing_snapshot_fails_op_but_server_stays_deleted(self) -> None:
        server = _server()
        fakes = Fakes(server=server, final=_FakeFinalCharge(raise_missing=True))
        with pytest.raises(DeleteOperationFailedError, match="no price snapshot"):
            await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        # the deletion fact holds; the billing gap goes to review
        assert fakes.servers[SERVER_ID].state is ServerLifecycleState.DELETED
        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.FAILED
        assert "no price snapshot" in (op.error or "")
        actions = [c.args[0].action for c in fakes.audit.append.call_args_list]
        assert "billing.final_charge_failed" in actions
        assert "server.delete_failed" in actions

    async def test_correlation_records_cap(self) -> None:
        server = _server()
        fakes = Fakes(server=server, final=_FakeFinalCharge(charged=0, capped=True))
        await fakes.make_service().request(USER_ID, SERVER_ID, IK)

        op = fakes.ops[OP_KEY]
        assert op.provider_response["charged_minor"] == 0
        assert op.provider_response["charge_capped"] is True


class TestWorkerRetry:
    async def test_worker_executes_requeued_delete(self) -> None:
        # first attempt requeued (provider down)
        server = _server()
        fakes = Fakes(
            server=server,
            provider=_FakeProvider(delete_error=ProviderUnavailable("down")),
        )
        service = fakes.make_service()
        first = await service.request(USER_ID, SERVER_ID, IK)
        assert first.requeued is True

        # the provider recovers; the worker picks the PENDING op up
        fakes.provider = _FakeProvider()
        counts = await fakes.make_worker(service).process_pending_deletes()

        assert counts == {"executed": 1, "requeued": 0, "failed": 0, "contended": 0}
        assert fakes.servers[SERVER_ID].state is ServerLifecycleState.DELETED
        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.COMPLETED
        assert op.attempts == 2
        assert len(fakes.final.calls) == 1

    async def test_worker_skips_empty_queue(self) -> None:
        fakes = Fakes()
        service = fakes.make_service()
        counts = await fakes.make_worker(service).process_pending_deletes()
        assert counts == {"executed": 0, "requeued": 0, "failed": 0, "contended": 0}
