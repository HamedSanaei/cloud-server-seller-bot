"""Tests for dead-letter / manual-review tooling (M11-004).

Acceptance: failed operations can be inspected/replayed safely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.domain import (
    InvalidOperationTransition,
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.tooling import (
    OperationNotRetryableError,
    OperationToolingError,
    OperationToolingService,
)
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
    UserStatus,
)

ADMIN_ID = uuid4()
USER_ID = uuid4()
RESOURCE_ID = uuid4()
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _admin() -> User:
    return User(
        id=ADMIN_ID,
        username="root",
        email="r@example.com",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )


def _plain_user() -> User:
    return User(
        id=USER_ID,
        username="alice",
        email="a@example.com",
        role=Role.USER,
        status=UserStatus.ACTIVE,
    )


def _op(
    key: str,
    status: OperationStatus,
    op_type: OperationType = OperationType.SERVER_CREATE,
    attempts: int = 3,
    resource_id: UUID = RESOURCE_ID,
) -> Operation:
    return Operation(
        id=uuid4(),
        operation_key=key,
        operation_type=op_type,
        resource_type="server",
        resource_id=resource_id,
        provider_key="hetzner",
        status=status,
        error="provider said no" if status is OperationStatus.FAILED else None,
        attempts=attempts,
        created_at=NOW,
        updated_at=NOW,
    )


def _server(server_id: UUID | None = None) -> CloudServer:
    return CloudServer(
        id=server_id or uuid4(),
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.MANUAL_REVIEW,
        contained_from=ServerLifecycleState.RUNNING,
    )


class FakeOpRepo:
    def __init__(self, ops: list[Operation]) -> None:
        self.ops = {o.operation_key: o for o in ops}
        self.saved: list[Operation] = []

    async def get_by_key(self, operation_key: str) -> Operation | None:
        return self.ops.get(operation_key)

    async def save(self, operation: Operation) -> Operation:
        self.saved.append(operation)
        self.ops[operation.operation_key] = operation
        return operation

    async def list_failed(
        self, *, operation_types: list[OperationType] | None = None, limit: int = 50
    ) -> list[Operation]:
        failed = [
            o
            for o in self.ops.values()
            if o.status is OperationStatus.FAILED
            and (operation_types is None or o.operation_type in operation_types)
        ]
        failed.sort(key=lambda o: o.updated_at or NOW, reverse=True)
        return failed[:limit]


class FakeServerRepo:
    def __init__(self, servers: list[CloudServer]) -> None:
        self.servers = servers

    async def list_manual_review(self) -> list[CloudServer]:
        return list(self.servers)


def _service(
    ops: list[Operation], servers: list[CloudServer]
) -> tuple[OperationToolingService, FakeOpRepo, AsyncMock]:
    op_repo = FakeOpRepo(ops)
    server_repo = FakeServerRepo(servers)
    audit = AsyncMock()
    return (
        OperationToolingService(op_repo, server_repo, audit),  # type: ignore[arg-type]
        op_repo,
        audit,
    )


class TestReopenTransition:
    def test_failed_reopens_to_pending(self) -> None:
        op = _op("k", OperationStatus.FAILED, attempts=3)
        op.reopen_for_retry()
        assert op.status is OperationStatus.PENDING
        assert op.attempts == 3  # attempts preserved; the next claim increments

    def test_reopen_then_worker_claim_works(self) -> None:
        op = _op("k", OperationStatus.FAILED, attempts=3)
        op.reopen_for_retry()
        op.mark_in_flight()
        assert op.status is OperationStatus.IN_FLIGHT
        assert op.attempts == 4  # history preserved + the new claim

    @pytest.mark.parametrize(
        "status",
        [OperationStatus.PENDING, OperationStatus.IN_FLIGHT, OperationStatus.COMPLETED],
    )
    def test_reopen_only_from_failed(self, status: OperationStatus) -> None:
        op = _op("k", status)
        with pytest.raises(InvalidOperationTransition):
            op.reopen_for_retry()


class TestRetrySafety:
    async def test_non_admin_cannot_retry(self) -> None:
        service, op_repo, audit = _service([_op("k", OperationStatus.FAILED)], [])
        with pytest.raises(PermissionDeniedError):
            await service.retry_failed("k", actor=_plain_user(), reason="x")
        assert op_repo.saved == []
        audit.append.assert_not_awaited()

    async def test_empty_reason_rejected(self) -> None:
        service, op_repo, _ = _service([_op("k", OperationStatus.FAILED)], [])
        with pytest.raises(OperationToolingError, match="reason"):
            await service.retry_failed("k", actor=_admin(), reason="  ")
        assert op_repo.saved == []

    async def test_unknown_key(self) -> None:
        service, _, _ = _service([], [])
        with pytest.raises(LookupError):
            await service.retry_failed("nope", actor=_admin(), reason="x")

    async def test_failed_reopens_saves_and_audits(self) -> None:
        failed = _op("server-create:abc", OperationStatus.FAILED, attempts=3)
        service, op_repo, audit = _service([failed], [])

        result = await service.retry_failed(
            "server-create:abc", actor=_admin(), reason="provider recovered"
        )

        assert result.status is OperationStatus.PENDING
        assert result.operation_key == "server-create:abc"  # same key -> provider dedupes
        assert result.attempts == 3
        assert len(op_repo.saved) == 1
        event = audit.append.call_args.args[0]
        assert event.action == "operation.retry"
        assert event.resource_id == "server-create:abc"
        assert event.reason == "provider recovered"
        assert event.metadata["attempts_before"] == "3"

    async def test_system_actor_allowed(self) -> None:
        service, _, _ = _service([_op("k", OperationStatus.FAILED)], [])
        result = await service.retry_failed("k", actor=None, reason="ops")
        assert result.status is OperationStatus.PENDING

    async def test_pending_is_idempotent_noop(self) -> None:
        pending = _op("k", OperationStatus.PENDING)
        service, op_repo, audit = _service([pending], [])
        result = await service.retry_failed("k", actor=_admin(), reason="x")
        assert result is pending
        assert op_repo.saved == []
        audit.append.assert_not_awaited()

    async def test_completed_never_replayed(self) -> None:
        done = _op("k", OperationStatus.COMPLETED)
        service, op_repo, audit = _service([done], [])
        with pytest.raises(OperationNotRetryableError, match="double-apply"):
            await service.retry_failed("k", actor=_admin(), reason="x")
        assert op_repo.saved == []
        audit.append.assert_not_awaited()

    async def test_in_flight_rejected(self) -> None:
        busy = _op("k", OperationStatus.IN_FLIGHT)
        service, op_repo, _ = _service([busy], [])
        with pytest.raises(OperationNotRetryableError, match="in flight"):
            await service.retry_failed("k", actor=_admin(), reason="x")
        assert op_repo.saved == []


class TestInspection:
    async def test_list_failed_filters_and_limits(self) -> None:
        ops = [
            _op("a", OperationStatus.FAILED, OperationType.SERVER_CREATE),
            _op("b", OperationStatus.FAILED, OperationType.REBOOT),
            _op("c", OperationStatus.COMPLETED),
            _op("d", OperationStatus.PENDING),
        ]
        service, _, _ = _service(ops, [])

        all_failed = await service.list_failed()
        assert {o.operation_key for o in all_failed} == {"a", "b"}

        creates = await service.list_failed(operation_types=[OperationType.SERVER_CREATE])
        assert [o.operation_key for o in creates] == ["a"]

        limited = await service.list_failed(limit=1)
        assert len(limited) == 1

    async def test_get_operation_by_key(self) -> None:
        op = _op("k", OperationStatus.FAILED)
        service, _, _ = _service([op], [])
        assert (await service.get_operation("k")) is op
        assert await service.get_operation("nope") is None


class TestReviewQueue:
    async def test_joins_servers_with_failed_operations(self) -> None:
        srv = _server()
        other = uuid4()
        ops = [
            _op("server-create:srv", OperationStatus.FAILED, resource_id=srv.id),
            _op("power_on:srv", OperationStatus.FAILED, OperationType.POWER_ON, resource_id=srv.id),
            _op("server-create:other", OperationStatus.FAILED, resource_id=other),
        ]
        service, _, _ = _service(ops, [srv])

        queue = await service.review_queue()

        assert len(queue) == 1
        assert queue[0].server.id == srv.id
        assert {o.operation_key for o in queue[0].failed_operations} == {
            "server-create:srv",
            "power_on:srv",
        }

    async def test_empty_queue(self) -> None:
        service, _, _ = _service([], [])
        assert await service.review_queue() == []
