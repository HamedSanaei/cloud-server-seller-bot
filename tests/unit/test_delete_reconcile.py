"""Tests for delete retry/reconcile (M07-008).

Acceptance: 404 is success; timeout ambiguity reconciles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from cloud_platform.modules.billing.service import FinalChargeResult
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.service import (
    DeleteCommandService,
    DeleteOperationExecutor,
    DeleteReconciliationOutcome,
    DeleteTimeoutReconciler,
    DeleteWorker,
    reconciled_delete_key,
)
from cloud_platform.modules.wallet.domain import Wallet
from cloud_platform.providers.base import ProviderServer
from cloud_platform.providers.errors import ProviderNotFound
from cloud_platform.providers.waiter import WaitOutcome, WaitResult

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
SERVER_ID = uuid4()
USER_ID = uuid4()
IK = "order-1"
OP_KEY = f"server-delete:{SERVER_ID}:del-1"


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
        idempotency_key=IK,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        provider_server_id="prov-1",
        quantum_seconds=3600,
    )
    defaults.update(kw)
    return CloudServer(**defaults)  # type: ignore[arg-type]


class _FakeProvider:
    def __init__(
        self, absent_after_delete: bool = True, delete_error: Exception | None = None
    ) -> None:
        self.absent = absent_after_delete
        self.delete_error = delete_error
        self.delete_calls: list[str] = []

    async def delete_server(self, provider_server_id: str, idempotency_key) -> None:
        self.delete_calls.append(provider_server_id)
        if self.delete_error is not None:
            raise self.delete_error

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        if self.absent:
            return None
        return ProviderServer(id=provider_server_id, name="srv", status="running")


class _FakeWaiter:
    async def wait_for(self, probe):
        return WaitResult(WaitOutcome.COMPLETED, 1, 0.1)


class _FakeFinalCharge:
    def __init__(self) -> None:
        self.calls: list[tuple[CloudServer, datetime]] = []

    async def charge_final(self, server: CloudServer, deleted_at: datetime) -> FinalChargeResult:
        self.calls.append((server, deleted_at))
        return FinalChargeResult(1000, False, "final:x", False, False)


class Fakes:
    def __init__(
        self,
        server: CloudServer | None = None,
        provider: _FakeProvider | None = None,
    ) -> None:
        self.servers: dict[UUID, CloudServer] = {(server or _server()).id: server or _server()}
        self.provider = provider or _FakeProvider()
        self.final = _FakeFinalCharge()
        self.ops: dict[str, Operation] = {}
        self.audit = AsyncMock()

    async def server_get(self, server_id) -> CloudServer | None:
        return self.servers.get(server_id)

    async def server_save(self, server: CloudServer) -> CloudServer:
        self.servers[server.id] = server
        return server

    async def server_list_deletion_in_progress(self) -> list[CloudServer]:
        return [
            s
            for s in self.servers.values()
            if s.state in (ServerLifecycleState.DELETE_REQUESTED, ServerLifecycleState.DELETING)
        ]

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
            if op.id == operation_id and op.status is OperationStatus.PENDING:
                op.mark_in_flight()
                return op
        return None

    async def ops_save(self, operation: Operation) -> Operation:
        operation.updated_at = NOW
        self.ops[operation.operation_key] = operation
        return operation

    async def ops_list_pending(self, types) -> list[Operation]:
        return [
            op
            for op in self.ops.values()
            if op.status is OperationStatus.PENDING and op.operation_type in types
        ]

    async def ops_list_in_flight(self, op_type) -> list[Operation]:
        return [
            op
            for op in self.ops.values()
            if op.status is OperationStatus.IN_FLIGHT and op.operation_type is op_type
        ]

    async def ops_list_failed(self, *, operation_types=None, limit=50) -> list[Operation]:
        return [
            op
            for op in self.ops.values()
            if op.status is OperationStatus.FAILED
            and (operation_types is None or op.operation_type in operation_types)
        ]

    async def wallet_get(self, user_id) -> Wallet | None:
        return Wallet(USER_ID, id=uuid4(), balance=10_000)

    def make_service(self) -> DeleteCommandService:
        h = self

        @dataclass
        class _ServerRepo:
            async def get(self, server_id):
                return await h.server_get(server_id)

            async def save(self, server):
                return await h.server_save(server)

            async def list_deletion_in_progress(self):
                return await h.server_list_deletion_in_progress()

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

            async def list_in_flight(self, op_type):
                return await h.ops_list_in_flight(op_type)

            async def list_failed(self, *, operation_types=None, limit=50):
                return await h.ops_list_failed(operation_types=operation_types, limit=limit)

        @dataclass
        class _WalletRepo:
            async def get(self, user_id):
                return await h.wallet_get(user_id)

        @dataclass
        class _HoldRepo:
            async def get_by_idempotency(self, wallet_id, key):
                return None

        @dataclass
        class _HoldService:
            async def release_hold(self, wallet_id, hold_id, key):
                raise AssertionError("no holds in this fixture")

        class _Registry:
            def get(self, key):
                if key != "hetzner":
                    raise KeyError(key)
                return h.provider

        op_repo = _OpRepo()
        executor = DeleteOperationExecutor(
            operation_repo=op_repo,
            server_repo=_ServerRepo(),
            provider_registry=_Registry(),
            final_charge=h.final,
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            wallet_repo=_WalletRepo(),
            audit_repo=h.audit,
            waiter=_FakeWaiter(),
        )
        return DeleteCommandService(
            server_repo=_ServerRepo(),
            operation_repo=op_repo,
            provider_registry=_Registry(),
            final_charge=h.final,
            hold_repo=_HoldRepo(),
            hold_service=_HoldService(),
            wallet_repo=_WalletRepo(),
            audit_repo=h.audit,
            executor=executor,
        )

    def make_worker(self, service: DeleteCommandService) -> DeleteWorker:
        return DeleteWorker(operation_repo=service._ops, executor=service._executor)

    def make_reconciler(self, clock_now: datetime = NOW) -> DeleteTimeoutReconciler:
        service = self.make_service()
        return DeleteTimeoutReconciler(
            operation_repo=service._ops,
            server_repo=service._servers,
            audit_repo=self.audit,
            clock=lambda: clock_now,
        )


def _in_flight_op(age: timedelta, status: OperationStatus = OperationStatus.IN_FLIGHT) -> Operation:
    op = Operation(
        id=uuid4(),
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_DELETE,
        resource_type="server",
        resource_id=SERVER_ID,
        provider_key="hetzner",
        status=OperationStatus.PENDING,
    )
    op.updated_at = NOW - age
    if status is OperationStatus.IN_FLIGHT:
        op.mark_in_flight()
    return op


class TestInFlightRequeue:
    async def test_stale_in_flight_is_requeued(self) -> None:
        server = _server(state=ServerLifecycleState.DELETING)
        fakes = Fakes(server=server)
        fakes.ops[OP_KEY] = _in_flight_op(timedelta(minutes=30))

        counts = await fakes.make_reconciler().reconcile()

        assert counts[DeleteReconciliationOutcome.REQUEUED_IN_FLIGHT] == 1
        op = fakes.ops[OP_KEY]
        assert op.status is OperationStatus.PENDING  # the worker re-runs it
        assert op.attempts == 1  # requeue does not consume an attempt
        actions = [c.args[0].action for c in fakes.audit.append.call_args_list]
        assert "server.delete_reconciled" in actions

    async def test_fresh_in_flight_is_skipped(self) -> None:
        server = _server(state=ServerLifecycleState.DELETING)
        fakes = Fakes(server=server)
        fakes.ops[OP_KEY] = _in_flight_op(timedelta(minutes=1))

        counts = await fakes.make_reconciler().reconcile()

        assert counts[DeleteReconciliationOutcome.SKIPPED] == 1
        assert fakes.ops[OP_KEY].status is OperationStatus.IN_FLIGHT

    async def test_in_flight_missing_server_row_fails(self) -> None:
        fakes = Fakes()
        fakes.servers.clear()  # the row is gone (external purge / drift)
        fakes.ops[OP_KEY] = _in_flight_op(timedelta(minutes=30))

        counts = await fakes.make_reconciler().reconcile()

        assert counts[DeleteReconciliationOutcome.FAILED] == 1
        assert fakes.ops[OP_KEY].status is OperationStatus.FAILED

    async def test_requeued_delete_survives_404_reentry(self) -> None:
        """End-to-end: crash requeue -> worker re-entry -> 404 is success."""
        server = _server(state=ServerLifecycleState.DELETING)
        fakes = Fakes(server=server)
        fakes.ops[OP_KEY] = _in_flight_op(timedelta(minutes=30))
        # the provider already applied the deletion before the crash: the
        # re-sent delete now 404s
        fakes.provider = _FakeProvider(delete_error=ProviderNotFound("gone"))
        service = fakes.make_service()

        await fakes.make_reconciler().reconcile()
        counts = await fakes.make_worker(service).process_pending_deletes()

        assert counts["executed"] == 1
        assert fakes.servers[SERVER_ID].state is ServerLifecycleState.DELETED
        assert fakes.ops[OP_KEY].status is OperationStatus.COMPLETED
        # the final segment was posted exactly once, on the re-entry
        assert len(fakes.final.calls) == 1


class TestLostOperation:
    async def test_lost_operation_is_recreated_and_deletion_finishes(self) -> None:
        # crash between the command's state save and the op create
        server = _server(state=ServerLifecycleState.DELETE_REQUESTED)
        fakes = Fakes(server=server)

        counts = await fakes.make_reconciler().reconcile()

        assert counts[DeleteReconciliationOutcome.RECREATED_OPERATION] == 1
        recreated = fakes.ops[reconciled_delete_key(SERVER_ID)]
        assert recreated is not None
        assert recreated.status is OperationStatus.PENDING
        assert recreated.operation_type is OperationType.SERVER_DELETE
        # the worker can now finish the deletion
        counts2 = await fakes.make_worker(fakes.make_service()).process_pending_deletes()
        assert counts2["executed"] == 1
        assert fakes.servers[SERVER_ID].state is ServerLifecycleState.DELETED

    async def test_server_with_existing_op_is_not_duplicated(self) -> None:
        server = _server(state=ServerLifecycleState.DELETE_REQUESTED)
        fakes = Fakes(server=server)
        fakes.ops[OP_KEY] = Operation(
            id=uuid4(),
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_DELETE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )  # PENDING: the worker already has it

        counts = await fakes.make_reconciler().reconcile()

        assert DeleteReconciliationOutcome.RECREATED_OPERATION not in counts
        assert len(fakes.ops) == 1  # no duplicate operation

    async def test_failed_op_also_prevents_duplication(self) -> None:
        server = _server(state=ServerLifecycleState.DELETE_REQUESTED)
        fakes = Fakes(server=server)
        fakes.ops[OP_KEY] = Operation(
            id=uuid4(),
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_DELETE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
            status=OperationStatus.FAILED,
            error="permanent",
        )

        counts = await fakes.make_reconciler().reconcile()

        assert DeleteReconciliationOutcome.RECREATED_OPERATION not in counts
        # the manual-retry tooling (operation.retry) owns this op
        assert len(fakes.ops) == 1

    async def test_deleting_server_is_ignored(self) -> None:
        # DELETING always has an operation (handled by the IN_FLIGHT path)
        server = _server(state=ServerLifecycleState.DELETING)
        fakes = Fakes(server=server)

        counts = await fakes.make_reconciler().reconcile()

        assert counts == {}

    async def test_no_ambiguous_deletions(self) -> None:
        fakes = Fakes()
        counts = await fakes.make_reconciler().reconcile()
        assert counts == {}
