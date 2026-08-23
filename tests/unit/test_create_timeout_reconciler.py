"""Tests for CreateTimeoutReconciler (M07-003).

Acceptance: an ambiguous timeout cannot create a duplicate server. The
reconciler re-resolves with the SAME idempotency key (provider dedupes) and
never issues a new key nor deletes a provider resource.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    CreateTimeoutReconciler,
    FirstLinuxImageSelector,
    ReconciliationOutcome,
    server_operation_key,
)
from cloud_platform.modules.wallet.domain import (
    Hold,
    HoldStatus,
    Wallet,
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

SERVER_ID = uuid4()
WALLET_ID = uuid4()
HOLD_ID = uuid4()
USER_ID = uuid4()
SPEC = ProvisioningSpec(plan_id="cx22", location_id="fsn1", currency="EUR")
OP_KEY = server_operation_key(SERVER_ID)

# Fixed "now" so elapsed-age math is deterministic.
NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
STALE = NOW - timedelta(hours=2)  # past any default timeout
FRESH = NOW - timedelta(seconds=30)  # within any default timeout


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

    async def claim(self, operation_id: UUID) -> Operation | None:
        op = await self.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Operation) -> Operation:
        # Mirror the real repository: a save stamps the last-moved time.
        operation.updated_at = NOW
        self.saved.append(operation)
        return operation


class FakeServerRepo:
    def __init__(
        self,
        servers: dict[UUID, CloudServer],
        specs: dict[UUID, ProvisioningSpec | None] | None = None,
    ) -> None:
        self.servers = dict(servers)
        self.specs = specs if specs is not None else {k: SPEC for k in servers}
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_requested(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.REQUESTED]

    async def list_provisioning(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.PROVISIONING]

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        return self.specs.get(server_id)

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class FakeProvider:
    key = "hetzner"
    capabilities = frozenset({Capability.COMPUTE})

    def __init__(self) -> None:
        self.create_calls: list[str] = []  # idempotency keys in call order
        self.get_calls: list[str] = []
        self.error: Exception | None = None
        self.get_error: Exception | None = None
        self.get_result: ProviderServer | None = ProviderServer(
            id="prov-123", name="srv-x", status="creating"
        )
        self.create_result = ProviderServer(id="prov-123", name="srv-x", status="creating")
        self.images = [
            ProviderImage(id="img-win", name="windows", os_family="windows", architecture="x86"),
            ProviderImage(id="img-linux", name="debian", os_family="linux", architecture="x86"),
        ]

    async def list_images(self) -> list[ProviderImage]:
        return self.images

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: Any
    ) -> ProviderServer:
        self.create_calls.append(idempotency_key.value)
        if self.error is not None:
            raise self.error
        return self.create_result

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        self.get_calls.append(provider_server_id)
        if self.get_error is not None:
            raise self.get_error
        return self.get_result


def _server(
    state: ServerLifecycleState = ServerLifecycleState.REQUESTED,
    idempotency_key: str | None = "cmd-key-1",
    provider_server_id: str | None = None,
) -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        idempotency_key=idempotency_key,
        provider_server_id=provider_server_id,
    )


class _Deps:
    def __init__(
        self,
        servers: dict[UUID, CloudServer],
        provider: FakeProvider,
        specs: dict[UUID, ProvisioningSpec | None] | None = None,
        **timeout: Any,
    ) -> None:
        self.ops = FakeOpRepo()
        self.server_repo = FakeServerRepo(servers, specs)
        self.provider = provider
        self.registry = ProviderRegistry()
        self.registry.register(provider)  # type: ignore[arg-type]
        self.wallets = AsyncMock()
        self.wallets.get = AsyncMock(
            return_value=Wallet(user_id=USER_ID, id=WALLET_ID, balance=100, currency="EUR")
        )
        self.holds = AsyncMock()
        self.holds.get_by_idempotency = AsyncMock(return_value=None)
        self.holds.release_hold = AsyncMock(return_value=None)
        self.audit = AsyncMock()
        self.audit.append = AsyncMock(side_effect=lambda e: e)
        kwargs: dict[str, Any] = dict(
            operation_repo=self.ops,  # type: ignore[arg-type]
            server_repo=self.server_repo,  # type: ignore[arg-type]
            provider_registry=self.registry,
            image_selector=FirstLinuxImageSelector(),
            wallet_repo=self.wallets,  # type: ignore[arg-type]
            hold_repo=self.holds,  # type: ignore[arg-type]
            audit_repo=self.audit,  # type: ignore[arg-type]
            clock=lambda: NOW,
        )
        kwargs.update(timeout)
        self.reconciler = CreateTimeoutReconciler(**kwargs)

    def audit_events(self) -> list:
        return [c.args[0] for c in self.audit.append.call_args_list]


def _in_flight_op(updated_at: datetime) -> Operation:
    op = Operation(
        id=uuid4(),
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=SERVER_ID,
        provider_key="hetzner",
    )
    op.mark_in_flight()
    op.updated_at = updated_at
    return op


def _hold() -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=107,
        currency="EUR",
        idempotency_key="server-create:cmd-key-1",
        id=HOLD_ID,
        status=HoldStatus.CREATED,
    )


class TestInFlightReconciliation:
    async def test_stale_in_flight_resolved_with_same_key(self) -> None:
        server = _server()
        provider = FakeProvider()
        deps = _Deps({server.id: server}, provider)
        deps.ops.add(_in_flight_op(STALE))

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.RECOVERED: 1}
        # provider called exactly once, with the SAME operation key
        assert provider.create_calls == [OP_KEY]
        op = deps.ops.by_key[OP_KEY]
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response is not None
        assert op.provider_response["idempotency_key"] == OP_KEY
        assert server.provider_server_id == "prov-123"
        assert server.state is ServerLifecycleState.PROVISIONING
        assert deps.audit_events()[0].action == "server.provisioning_recovered"

    async def test_fresh_in_flight_is_skipped(self) -> None:
        server = _server()
        provider = FakeProvider()
        deps = _Deps({server.id: server}, provider)
        deps.ops.add(_in_flight_op(FRESH))

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.SKIPPED: 1}
        assert provider.create_calls == []

    async def test_never_issues_a_new_key_across_retries(self) -> None:
        """Even if a re-send fails and is retried, the key never changes."""
        server = _server()
        provider = FakeProvider()
        provider.error = ProviderUnavailable("503")
        deps = _Deps({server.id: server}, provider)
        deps.ops.add(_in_flight_op(STALE))

        first = await deps.reconciler.reconcile()
        assert first == {ReconciliationOutcome.REQUEUED: 1}
        # still IN_FLIGHT? No — requeued to PENDING. Re-add as a fresh IN_FLIGHT
        # attempt (same key) and reconcile again after the provider recovers.
        provider.error = None
        op = deps.ops.by_key[OP_KEY]
        assert op.status is OperationStatus.PENDING
        op.mark_in_flight()
        op.updated_at = STALE

        second = await deps.reconciler.reconcile()
        assert second == {ReconciliationOutcome.RECOVERED: 1}
        # BOTH sends used the identical idempotency key — no duplicate possible
        assert provider.create_calls == [OP_KEY, OP_KEY]
        assert len(set(provider.create_calls)) == 1

    async def test_retryable_error_requeues(self) -> None:
        server = _server()
        provider = FakeProvider()
        provider.error = ProviderUnavailable("503")
        deps = _Deps({server.id: server}, provider)
        deps.ops.add(_in_flight_op(STALE))

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.REQUEUED: 1}
        op = deps.ops.by_key[OP_KEY]
        assert op.status is OperationStatus.PENDING
        assert op.error == "503"
        assert server.state is ServerLifecycleState.REQUESTED
        assert deps.audit_events()[0].action == "server.provisioning_requeued"

    async def test_permanent_error_fails_and_releases_hold(self) -> None:
        server = _server()
        provider = FakeProvider()
        provider.error = ProviderAuthError("401")
        deps = _Deps({server.id: server}, provider)
        deps.ops.add(_in_flight_op(STALE))
        deps.holds.get_by_idempotency = AsyncMock(return_value=_hold())

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.FAILED: 1}
        op = deps.ops.by_key[OP_KEY]
        assert op.status is OperationStatus.FAILED
        assert server.state is ServerLifecycleState.ERROR
        deps.holds.release_hold.assert_awaited_once_with(HOLD_ID)
        assert deps.audit_events()[0].action == "server.provisioning_failed"

    async def test_missing_server_row_fails(self) -> None:
        provider = FakeProvider()
        deps = _Deps({}, provider)
        deps.ops.add(_in_flight_op(STALE))

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.FAILED: 1}
        assert deps.ops.by_key[OP_KEY].status is OperationStatus.FAILED
        assert provider.create_calls == []

    async def test_server_left_create_window_fails_and_refunds(self) -> None:
        server = _server(state=ServerLifecycleState.DELETING)
        provider = FakeProvider()
        deps = _Deps({server.id: server}, provider)
        deps.ops.add(_in_flight_op(STALE))
        deps.holds.get_by_idempotency = AsyncMock(return_value=_hold())

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.FAILED: 1}
        assert deps.ops.by_key[OP_KEY].status is OperationStatus.FAILED
        deps.holds.release_hold.assert_awaited_once_with(HOLD_ID)


class TestProvisioningReconciliation:
    async def test_stuck_provisioning_with_live_remote_goes_to_review(self) -> None:
        server = _server(state=ServerLifecycleState.PROVISIONING, provider_server_id="prov-123")
        provider = FakeProvider()
        provider.get_result = ProviderServer(id="prov-123", name="srv-x", status="creating")
        deps = _Deps({server.id: server}, provider)
        op = await deps.ops.get_or_create(
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        op.mark_in_flight()
        op.complete(
            {
                "provider_server_id": "prov-123",
                "provider_status": "creating",
                "idempotency_key": OP_KEY,
            }
        )
        op.updated_at = STALE

        counts = await deps.reconciler.reconcile()

        # uncertain (exists but not finished): contained, not deleted, not re-created
        assert counts == {ReconciliationOutcome.MARKED_FOR_REVIEW: 1}
        assert server.state is ServerLifecycleState.MANUAL_REVIEW
        assert provider.create_calls == []
        assert provider.get_calls == ["prov-123"]
        assert deps.audit_events()[0].action == "server.provisioning_review"

    async def test_vanished_remote_fails_and_releases_hold(self) -> None:
        server = _server(state=ServerLifecycleState.PROVISIONING, provider_server_id="prov-123")
        provider = FakeProvider()
        provider.get_result = None  # provider resource gone
        deps = _Deps({server.id: server}, provider)
        deps.holds.get_by_idempotency = AsyncMock(return_value=_hold())
        op = await deps.ops.get_or_create(
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        op.mark_in_flight()
        op.complete({"provider_server_id": "prov-123", "idempotency_key": OP_KEY})
        op.updated_at = STALE

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.FAILED: 1}
        assert server.state is ServerLifecycleState.ERROR
        deps.holds.release_hold.assert_awaited_once_with(HOLD_ID)
        assert deps.audit_events()[0].action == "server.provisioning_lost"
        assert provider.create_calls == []

    async def test_get_server_error_is_left_unchanged(self) -> None:
        server = _server(state=ServerLifecycleState.PROVISIONING, provider_server_id="prov-123")
        provider = FakeProvider()
        provider.get_error = ProviderUnavailable("503")
        deps = _Deps({server.id: server}, provider)
        op = await deps.ops.get_or_create(
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        op.mark_in_flight()
        op.complete({"provider_server_id": "prov-123", "idempotency_key": OP_KEY})
        op.updated_at = STALE

        counts = await deps.reconciler.reconcile()

        # cannot conclude the resource is gone; retry next round
        assert counts == {ReconciliationOutcome.LEFT_UNCHANGED: 1}
        assert server.state is ServerLifecycleState.PROVISIONING

    async def test_missing_operation_goes_to_review(self) -> None:
        server = _server(state=ServerLifecycleState.PROVISIONING, provider_server_id="prov-123")
        provider = FakeProvider()
        deps = _Deps({server.id: server}, provider)  # no operation for this key

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.MARKED_FOR_REVIEW: 1}
        assert server.state is ServerLifecycleState.MANUAL_REVIEW

    async def test_fresh_provisioning_is_skipped(self) -> None:
        server = _server(state=ServerLifecycleState.PROVISIONING, provider_server_id="prov-123")
        provider = FakeProvider()
        deps = _Deps({server.id: server}, provider)
        op = await deps.ops.get_or_create(
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        op.mark_in_flight()
        op.complete({"provider_server_id": "prov-123", "idempotency_key": OP_KEY})
        op.updated_at = FRESH

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.SKIPPED: 1}
        assert provider.get_calls == []


class TestRequestedOperationRecovery:
    async def test_missing_operation_is_recreated_pending(self) -> None:
        server = _server()
        provider = FakeProvider()
        deps = _Deps({server.id: server}, provider)  # no operation

        counts = await deps.reconciler.reconcile()

        assert counts == {ReconciliationOutcome.RECREATED_OPERATION: 1}
        recreated = deps.ops.by_key[OP_KEY]
        assert recreated.status is OperationStatus.PENDING
        assert provider.create_calls == []  # the worker will claim it later

    async def test_existing_pending_operation_is_left_alone(self) -> None:
        server = _server()
        provider = FakeProvider()
        deps = _Deps({server.id: server}, provider)
        op = await deps.ops.get_or_create(
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        assert op.status is OperationStatus.PENDING

        counts = await deps.reconciler.reconcile()

        assert counts == {}  # nothing to do
        assert provider.create_calls == []


class TestValidation:
    def test_non_positive_timeout_rejected(self) -> None:
        provider = FakeProvider()
        with pytest.raises(ValueError, match="positive"):
            _Deps({}, provider, in_flight_timeout=timedelta(0))
        with pytest.raises(ValueError, match="positive"):
            _Deps({}, provider, provisioning_timeout=timedelta(-1))
