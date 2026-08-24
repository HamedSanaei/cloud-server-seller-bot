"""Tests for provisioning progress notifications (M08-006).

Acceptance: the user receives the final success/error exactly once.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    CloudServer,
    ProvisioningSpec,
    ServerLifecycleState,
)
from cloud_platform.modules.notifications.domain import (
    ProvisioningEvent,
    ProvisioningEventKind,
    ProvisioningProgressService,
)
from cloud_platform.modules.operations.domain import Operation, OperationType
from cloud_platform.modules.operations.service import (
    FirstLinuxImageSelector,
    ProvisioningOutcome,
    ProvisioningWorker,
)
from cloud_platform.modules.wallet.domain import Hold, HoldStatus, Wallet
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    ProviderImage,
    ProviderServer,
)
from cloud_platform.providers.errors import ProviderAuthError
from cloud_platform.providers.registry import ProviderRegistry

SERVER_ID = uuid4()
USER_ID = uuid4()
WALLET_ID = uuid4()
HOLD_ID = uuid4()
SPEC = ProvisioningSpec(plan_id="cx22", location_id="fsn1", currency="EUR")
OP_KEY = f"server-create:{SERVER_ID}"


def _server(
    state: ServerLifecycleState = ServerLifecycleState.REQUESTED,
) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        idempotency_key="cmd-key-1",
    )


# --------------------------------------------------------------------------
# The exactly-once log + service
# --------------------------------------------------------------------------


class FakeLog:
    """In-memory stand-in for the (server_id, kind) unique-constraint log."""

    def __init__(self) -> None:
        self.records: list[tuple[UUID, UUID, ProvisioningEventKind, str | None]] = []

    async def record_final(
        self, user_id: UUID, server_id: UUID, kind: ProvisioningEventKind, detail: str | None
    ) -> bool:
        for _u, s, k, _d in self.records:
            if s == server_id and k == kind:
                return False
        self.records.append((user_id, server_id, kind, detail))
        return True


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[ProvisioningEvent] = []

    async def send(self, event: ProvisioningEvent) -> None:
        self.events.append(event)


def _service(log: FakeLog, notifier: RecordingNotifier) -> ProvisioningProgressService:
    return ProvisioningProgressService(notifier, log)


class TestEventKind:
    def test_final_kinds(self) -> None:
        assert ProvisioningEventKind.STARTED.is_final is False
        assert ProvisioningEventKind.SUCCESS.is_final is True
        assert ProvisioningEventKind.FAILED.is_final is True


class TestExactlyOnce:
    async def test_success_delivered_exactly_once(self) -> None:
        log, notifier = FakeLog(), RecordingNotifier()
        service = _service(log, notifier)
        server = _server()

        first = await service.succeeded(server, "provider server prov-1")
        second = await service.succeeded(server)  # retry / second reconciler round
        third = await service.succeeded(server)

        assert first is True
        assert second is False
        assert third is False
        assert len(notifier.events) == 1
        (event,) = notifier.events
        assert event.kind is ProvisioningEventKind.SUCCESS
        assert event.user_id == USER_ID
        assert event.server_id == SERVER_ID
        assert event.detail == "provider server prov-1"

    async def test_failure_delivered_exactly_once(self) -> None:
        log, notifier = FakeLog(), RecordingNotifier()
        service = _service(log, notifier)
        server = _server()

        assert await service.failed(server, "quota") is True
        assert await service.failed(server, "quota") is False
        assert len(notifier.events) == 1
        assert notifier.events[0].kind is ProvisioningEventKind.FAILED

    async def test_success_and_failure_are_separate_kinds(self) -> None:
        # The log dedupes per (server, kind): a success already delivered does
        # not suppress a later final of a different kind (and vice versa).
        log, notifier = FakeLog(), RecordingNotifier()
        service = _service(log, notifier)
        server = _server()

        assert await service.succeeded(server) is True
        assert await service.failed(server, "later failure") is True
        assert len(notifier.events) == 2

    async def test_different_servers_are_independent(self) -> None:
        log, notifier = FakeLog(), RecordingNotifier()
        service = _service(log, notifier)
        s1 = _server()
        s2 = _server()
        s2.id = uuid4()

        assert await service.succeeded(s1) is True
        assert await service.succeeded(s2) is True
        assert len(notifier.events) == 2

    async def test_started_is_not_deduplicated(self) -> None:
        log, notifier = FakeLog(), RecordingNotifier()
        service = _service(log, notifier)
        server = _server()

        await service.started(server, "provider server prov-1")
        await service.started(server, "provider server prov-1")

        assert len(notifier.events) == 2
        assert all(e.kind is ProvisioningEventKind.STARTED for e in notifier.events)
        assert log.records == []  # progress events never touch the final log


class TestLoggingNotifier:
    async def test_logs_without_raising(self, caplog: pytest.LogCaptureFixture) -> None:
        from cloud_platform.modules.notifications.domain import _LoggingNotifier

        with caplog.at_level(logging.INFO):
            await _LoggingNotifier().send(
                ProvisioningEvent(USER_ID, SERVER_ID, ProvisioningEventKind.FAILED, "boom")
            )
        assert "provisioning failed" in caplog.text
        assert "boom" in caplog.text


# --------------------------------------------------------------------------
# Repository: the (server_id, kind) constraint is the exactly-once guard
# --------------------------------------------------------------------------


class FakeSession:
    def __init__(self, fail_on_insert: bool) -> None:
        self._fail = fail_on_insert
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def add(self, model: Any) -> None:
        self.added.append(model)

    async def commit(self) -> None:
        if self._fail:
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _Ctx:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class TestSqlAlchemyLog:
    async def test_first_record_wins(self) -> None:
        from cloud_platform.modules.notifications.repository import (
            SqlAlchemyProvisioningNotificationLogRepository,
        )

        session = FakeSession(fail_on_insert=False)
        repo = SqlAlchemyProvisioningNotificationLogRepository(lambda: _Ctx(session))
        assert await repo.record_final(USER_ID, SERVER_ID, ProvisioningEventKind.SUCCESS, "d")
        assert session.committed is True
        assert len(session.added) == 1
        row = session.added[0]
        assert row.user_id == USER_ID
        assert row.server_id == SERVER_ID
        assert row.kind == "success"

    async def test_duplicate_is_a_noop(self) -> None:
        from cloud_platform.modules.notifications.repository import (
            SqlAlchemyProvisioningNotificationLogRepository,
        )

        session = FakeSession(fail_on_insert=True)
        repo = SqlAlchemyProvisioningNotificationLogRepository(lambda: _Ctx(session))
        assert (
            await repo.record_final(USER_ID, SERVER_ID, ProvisioningEventKind.FAILED, "d") is False
        )
        assert session.rolled_back is True

    async def test_non_final_rejected(self) -> None:
        from cloud_platform.modules.notifications.repository import (
            SqlAlchemyProvisioningNotificationLogRepository,
        )

        repo = SqlAlchemyProvisioningNotificationLogRepository(lambda: _Ctx(FakeSession(False)))
        with pytest.raises(ValueError, match="final"):
            await repo.record_final(USER_ID, SERVER_ID, ProvisioningEventKind.STARTED, None)


# --------------------------------------------------------------------------
# Worker integration: started / final success / final failure are wired
# --------------------------------------------------------------------------


class FakeOpRepo:
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

    async def claim(self, operation_id: UUID) -> Operation | None:
        for op in self.ops.values():
            if op.id == operation_id:
                if op.status.name != "PENDING":
                    return None
                op.mark_in_flight()
                return op
        return None

    async def save(self, operation: Operation) -> Operation:
        return operation

    async def list_in_flight(self, operation_type: OperationType) -> list[Operation]:
        return []


class FakeServerRepo:
    def __init__(self, server: CloudServer) -> None:
        self.server = server
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.server if self.server.id == server_id else None

    async def list_requested(self) -> list[CloudServer]:
        return []

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        return SPEC

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class FakeProvider:
    key = "hetzner"
    capabilities = frozenset({Capability.COMPUTE})

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def list_images(self) -> list[ProviderImage]:
        return [ProviderImage(id="img-linux", name="debian", os_family="linux", architecture="x86")]

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: Any
    ) -> ProviderServer:
        if self.error is not None:
            raise self.error
        return ProviderServer(id="prov-123", name="srv", status="creating")


class _WorkerDeps:
    def __init__(
        self,
        server: CloudServer,
        provider: FakeProvider,
        progress: ProvisioningProgressService | None,
    ) -> None:
        from unittest.mock import AsyncMock

        self.ops = FakeOpRepo()
        self.server_repo = FakeServerRepo(server)
        self.registry = ProviderRegistry()
        self.registry.register(provider)  # type: ignore[arg-type]
        self.wallets = AsyncMock()
        self.wallets.get = AsyncMock(
            return_value=Wallet(user_id=USER_ID, id=WALLET_ID, balance=100, currency="EUR")
        )
        self.holds = AsyncMock()
        self.holds.get_by_idempotency = AsyncMock(
            return_value=Hold(
                wallet_id=WALLET_ID,
                amount=107,
                currency="EUR",
                idempotency_key="server-create:cmd-key-1",
                id=HOLD_ID,
                status=HoldStatus.CREATED,
            )
        )
        self.holds.release_hold = AsyncMock(return_value=None)
        self.audit = AsyncMock()
        self.audit.append = AsyncMock(side_effect=lambda e: e)
        self.worker = ProvisioningWorker(
            operation_repo=self.ops,  # type: ignore[arg-type]
            server_repo=self.server_repo,  # type: ignore[arg-type]
            provider_registry=self.registry,
            image_selector=FirstLinuxImageSelector(),
            wallet_repo=self.wallets,  # type: ignore[arg-type]
            hold_repo=self.holds,  # type: ignore[arg-type]
            audit_repo=self.audit,  # type: ignore[arg-type]
            progress=progress,
        )


class TestWorkerIntegration:
    async def test_no_progress_service_changes_nothing(self) -> None:
        server = _server()
        deps = _WorkerDeps(server, FakeProvider(), None)
        outcome = await deps.worker.process_server(SERVER_ID)
        assert outcome is ProvisioningOutcome.PROVISIONED
        assert deps.holds.release_hold.await_count == 0

    async def test_started_and_success_emitted_on_happy_path(self) -> None:
        server = _server()
        log, notifier = FakeLog(), RecordingNotifier()
        deps = _WorkerDeps(server, FakeProvider(), _service(log, notifier))

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.PROVISIONED
        # the provider did not finish synchronously: only the started event,
        # no final yet (the wait/reconciler decides the final outcome)
        assert [e.kind for e in notifier.events] == [ProvisioningEventKind.STARTED]
        assert notifier.events[0].detail == "provider server prov-123"
        assert log.records == []  # no final recorded yet

    async def test_success_emitted_when_wait_completes(self) -> None:
        from cloud_platform.providers.waiter import WaitOutcome, WaitResult

        server = _server()
        log, notifier = FakeLog(), RecordingNotifier()
        deps = _WorkerDeps(server, FakeProvider(), _service(log, notifier))

        class _Waiter:
            async def wait_for(self, probe: Any) -> WaitResult:
                return WaitResult(WaitOutcome.COMPLETED, polls=2, elapsed_seconds=3.5, detail=None)

        deps.worker._waiter = _Waiter()
        await deps.worker.process_server(SERVER_ID)

        assert server.state is ServerLifecycleState.RUNNING
        assert [e.kind for e in notifier.events] == [
            ProvisioningEventKind.STARTED,
            ProvisioningEventKind.SUCCESS,
        ]
        assert len(log.records) == 1
        assert log.records[0][2] is ProvisioningEventKind.SUCCESS

    async def test_failure_emitted_on_permanent_provider_error(self) -> None:
        server = _server()
        log, notifier = FakeLog(), RecordingNotifier()
        deps = _WorkerDeps(
            server,
            FakeProvider(error=ProviderAuthError("bad key")),
            _service(log, notifier),
        )

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.FAILED
        assert server.state is ServerLifecycleState.ERROR
        assert [e.kind for e in notifier.events] == [ProvisioningEventKind.FAILED]
        assert "bad key" in (notifier.events[0].detail or "")
        assert len(log.records) == 1
        assert log.records[0][2] is ProvisioningEventKind.FAILED
        # the hold is released with the failure
        assert deps.holds.release_hold.await_count == 1
