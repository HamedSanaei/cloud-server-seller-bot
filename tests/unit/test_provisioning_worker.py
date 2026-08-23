"""Tests for the provisioning worker (M07-002).

Acceptance: calls provider once per operation intent and records correlation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

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
    FirstLinuxImageSelector,
    ProvisioningOutcome,
    ProvisioningWorker,
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
OP_KEY = f"server-create:{SERVER_ID}"


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

    async def get(self, operation_id: UUID) -> Operation | None:
        for op in self.ops.values():
            if op.id == operation_id:
                return op
        return None

    async def claim(self, operation_id: UUID) -> Operation | None:
        op = await self.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Operation) -> Operation:
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

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        return self.specs.get(server_id)

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class FakeProvider:
    key = "hetzner"
    capabilities = frozenset({Capability.COMPUTE})

    def __init__(self, images: list[ProviderImage] | None = None) -> None:
        self.create_calls = 0
        self.last_key: str | None = None
        self.last_request: CreateServerRequest | None = None
        self.error: Exception | None = None
        self.result = ProviderServer(id="prov-123", name="srv-x", status="creating")
        self.images = (
            images
            if images is not None
            else [
                ProviderImage(
                    id="img-win", name="windows", os_family="windows", architecture="x86"
                ),
                ProviderImage(id="img-linux", name="debian", os_family="linux", architecture="x86"),
            ]
        )

    async def list_images(self) -> list[ProviderImage]:
        return self.images

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: Any
    ) -> ProviderServer:
        self.create_calls += 1
        self.last_key = idempotency_key.value
        self.last_request = request
        if self.error is not None:
            raise self.error
        return self.result


def _server(
    state: ServerLifecycleState = ServerLifecycleState.REQUESTED,
    idempotency_key: str | None = "cmd-key-1",
    server_id: UUID = SERVER_ID,
) -> CloudServer:
    return CloudServer(
        id=server_id,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=state,
        idempotency_key=idempotency_key,
    )


class _Deps:
    def __init__(
        self,
        servers: dict[UUID, CloudServer],
        provider: FakeProvider,
        specs: dict[UUID, ProvisioningSpec | None] | None = None,
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
        self.worker = ProvisioningWorker(
            operation_repo=self.ops,  # type: ignore[arg-type]
            server_repo=self.server_repo,  # type: ignore[arg-type]
            provider_registry=self.registry,
            image_selector=FirstLinuxImageSelector(),
            wallet_repo=self.wallets,  # type: ignore[arg-type]
            hold_repo=self.holds,  # type: ignore[arg-type]
            audit_repo=self.audit,  # type: ignore[arg-type]
        )

    def audit_events(self) -> list:
        return [c.args[0] for c in self.audit.append.call_args_list]

    async def pre_create_op(self) -> Operation:
        return await self.ops.get_or_create(
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )


def _hold() -> Hold:
    return Hold(
        wallet_id=WALLET_ID,
        amount=107,
        currency="EUR",
        idempotency_key="server-create:cmd-key-1",
        id=HOLD_ID,
        status=HoldStatus.CREATED,
    )


class TestHappyPath:
    async def test_provisions_once_and_records_correlation(self) -> None:
        server = _server()
        deps = _Deps({server.id: server}, FakeProvider())

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.PROVISIONED
        # exactly one provider call, keyed by the operation intent key
        assert deps.provider.create_calls == 1
        assert deps.provider.last_key == OP_KEY
        # request built from the pinned catalog spec + linux image
        assert deps.provider.last_request is not None
        assert deps.provider.last_request.plan_id == "cx22"
        assert deps.provider.last_request.location_id == "fsn1"
        assert deps.provider.last_request.image_id == "img-linux"
        assert deps.provider.last_request.labels == {"platform_server_id": str(SERVER_ID)}
        # operation completed with the correlation
        op = deps.ops.ops[OP_KEY]
        assert op.status is OperationStatus.COMPLETED
        assert op.attempts == 1
        assert op.provider_response == {
            "provider_server_id": "prov-123",
            "provider_status": "creating",
            "idempotency_key": OP_KEY,
        }
        # server row advanced to PROVISIONING with the provider id
        assert server.provider_server_id == "prov-123"
        assert server.state is ServerLifecycleState.PROVISIONING
        assert len(deps.server_repo.saved) == 1
        # audit
        event = deps.audit_events()[0]
        assert event.action == "server.provisioning_started"
        assert event.actor_type.value == "system"
        assert event.metadata["provider_server_id"] == "prov-123"
        assert event.metadata["idempotency_key"] == OP_KEY
        deps.holds.release_hold.assert_not_awaited()

    async def test_rerun_is_idempotent_no_second_call(self) -> None:
        server = _server()
        deps = _Deps({server.id: server}, FakeProvider())

        first = await deps.worker.process_server(SERVER_ID)
        second = await deps.worker.process_server(SERVER_ID)

        assert first is ProvisioningOutcome.PROVISIONED
        assert second is ProvisioningOutcome.ALREADY_PROVISIONED
        assert deps.provider.create_calls == 1


class TestClaimRace:
    async def test_in_flight_operation_is_skipped(self) -> None:
        server = _server()
        deps = _Deps({server.id: server}, FakeProvider())
        op = await deps.pre_create_op()
        op.mark_in_flight()  # another worker owns the attempt

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.SKIPPED_IN_FLIGHT
        assert deps.provider.create_calls == 0

    async def test_claim_lost_is_skipped(self) -> None:
        server = _server()
        deps = _Deps({server.id: server}, FakeProvider())
        deps.ops.claim = AsyncMock(return_value=None)  # type: ignore[method-assign]

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.SKIPPED_IN_FLIGHT
        assert deps.provider.create_calls == 0


class TestFailures:
    async def test_retryable_error_requeues_same_key(self) -> None:
        server = _server()
        provider = FakeProvider()
        provider.error = ProviderUnavailable("503")
        deps = _Deps({server.id: server}, provider)

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.REQUEUED
        op = deps.ops.ops[OP_KEY]
        assert op.status is OperationStatus.PENDING
        assert op.error == "503"
        assert op.attempts == 1
        assert server.state is ServerLifecycleState.REQUESTED
        assert len(deps.server_repo.saved) == 0
        assert deps.audit_events()[0].action == "server.provisioning_requeued"

    async def test_requeue_then_success_uses_same_key(self) -> None:
        server = _server()
        provider = FakeProvider()
        provider.error = ProviderUnavailable("503")
        deps = _Deps({server.id: server}, provider)

        assert await deps.worker.process_server(SERVER_ID) is ProvisioningOutcome.REQUEUED
        provider.error = None  # provider recovers
        assert await deps.worker.process_server(SERVER_ID) is ProvisioningOutcome.PROVISIONED
        # two sends, but the SAME idempotency key: the provider deduplicates
        assert provider.create_calls == 2
        assert provider.last_key == OP_KEY

    async def test_permanent_error_fails_and_releases_hold(self) -> None:
        server = _server()
        provider = FakeProvider()
        provider.error = ProviderAuthError("401 bad token")
        deps = _Deps({server.id: server}, provider)
        deps.holds.get_by_idempotency = AsyncMock(return_value=_hold())

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.FAILED
        op = deps.ops.ops[OP_KEY]
        assert op.status is OperationStatus.FAILED
        assert op.error == "401 bad token"
        assert server.state is ServerLifecycleState.ERROR
        deps.holds.release_hold.assert_awaited_once_with(HOLD_ID)
        assert deps.audit_events()[0].action == "server.provisioning_failed"

    async def test_missing_spec_fails(self) -> None:
        server = _server()
        deps = _Deps({server.id: server}, FakeProvider(), specs={SERVER_ID: None})

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.FAILED
        assert "catalog offer missing" in str(deps.ops.ops[OP_KEY].error)
        assert deps.provider.create_calls == 0

    async def test_unknown_provider_fails(self) -> None:
        server = _server()
        deps = _Deps({server.id: server}, FakeProvider())
        deps.worker._registry = ProviderRegistry()  # empty registry

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.FAILED
        assert "unknown provider" in str(deps.ops.ops[OP_KEY].error)
        assert deps.provider.create_calls == 0

    async def test_no_image_fails(self) -> None:
        server = _server()
        provider = FakeProvider(
            images=[
                ProviderImage(id="img-win", name="windows", os_family="windows", architecture="x86")
            ]
        )
        deps = _Deps({server.id: server}, provider)

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.FAILED
        assert "no image" in str(deps.ops.ops[OP_KEY].error)
        assert deps.provider.create_calls == 0


class TestRecovery:
    async def test_completed_operation_repairs_row(self) -> None:
        server = _server()  # REQUESTED, no provider id (crash before save)
        deps = _Deps({server.id: server}, FakeProvider())
        op = await deps.pre_create_op()
        op.mark_in_flight()
        op.complete(
            {
                "provider_server_id": "prov-777",
                "provider_status": "running",
                "idempotency_key": OP_KEY,
            }
        )

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.ALREADY_PROVISIONED
        assert deps.provider.create_calls == 0  # no provider call on recovery
        assert server.provider_server_id == "prov-777"
        assert server.state is ServerLifecycleState.PROVISIONING

    async def test_non_intent_states_are_skipped(self) -> None:
        server = _server(state=ServerLifecycleState.RUNNING)
        deps = _Deps({server.id: server}, FakeProvider())

        outcome = await deps.worker.process_server(SERVER_ID)

        assert outcome is ProvisioningOutcome.SKIPPED_STATE
        assert deps.ops.ops == {}
        assert deps.provider.create_calls == 0

    async def test_missing_server_is_skipped(self) -> None:
        server = _server()
        deps = _Deps({server.id: server}, FakeProvider())

        outcome = await deps.worker.process_server(uuid4())

        assert outcome is ProvisioningOutcome.SKIPPED_STATE
        assert deps.provider.create_calls == 0


class TestRunOnce:
    async def test_processes_requested_batch(self) -> None:
        first = _server()
        second_id = uuid4()
        servers = {
            first.id: first,
            second_id: _server(server_id=second_id, idempotency_key="k2"),
            uuid4(): _server(state=ServerLifecycleState.RUNNING),
        }
        provider = FakeProvider()
        deps = _Deps(servers, provider)

        counts = await deps.worker.run_once(limit=10)

        assert counts == {ProvisioningOutcome.PROVISIONED: 2}
        assert provider.create_calls == 2
        # each intent got its own operation key
        assert set(deps.ops.ops.keys()) == {
            f"server-create:{first.id}",
            f"server-create:{second_id}",
        }
        for op in deps.ops.ops.values():
            assert op.provider_response is not None
            assert op.provider_response["idempotency_key"] == op.operation_key
