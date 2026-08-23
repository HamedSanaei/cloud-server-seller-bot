"""Tests for the per-account provisioning concurrency limit (M07-011).

Acceptance: protects provider quota/rate limit.
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
    FirstLinuxImageSelector,
    ProvisioningOutcome,
    ProvisioningWorker,
)
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    ProviderImage,
    ProviderServer,
)
from cloud_platform.providers.registry import ProviderRegistry

USER_ID = uuid4()
ACCOUNT_A = uuid4()
ACCOUNT_B = uuid4()
SPEC = ProvisioningSpec(plan_id="cx22", location_id="fsn1", currency="EUR")


def _server(
    server_id: UUID,
    state: ServerLifecycleState = ServerLifecycleState.REQUESTED,
    provider_key: str = "hetzner",
    account: UUID = ACCOUNT_A,
) -> CloudServer:
    return CloudServer(
        id=server_id,
        user_id=USER_ID,
        provider_key=provider_key,
        provider_account_id=account,
        state=state,
    )


def _busy_server(server_id: UUID, account: UUID = ACCOUNT_A) -> CloudServer:
    """A server mid-create (PROVISIONING) whose op is currently in flight."""
    return _server(server_id, state=ServerLifecycleState.PROVISIONING, account=account)


class FakeOpRepo:
    def __init__(self) -> None:
        self.ops: dict[str, Operation] = {}
        self.in_flight: list[Operation] = []

    def seed_in_flight(self, server: CloudServer, key: str) -> Operation:
        op = Operation(
            id=uuid4(),
            operation_key=key,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=server.id,
            provider_key=server.provider_key,
            status=OperationStatus.IN_FLIGHT,
            attempts=1,
        )
        self.in_flight.append(op)
        self.ops[key] = op
        return op

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
                attempts=0,
            )
            self.ops[operation_key] = op
        return op

    async def get(self, operation_id: UUID) -> Operation | None:
        for op in self.ops.values():
            if op.id == operation_id:
                return op
        return None

    async def get_by_key(self, operation_key: str) -> Operation | None:
        return self.ops.get(operation_key)

    async def list_in_flight(self, operation_type: OperationType) -> list[Operation]:
        return [op for op in self.in_flight if op.operation_type is operation_type]

    async def list_pending(self, types: tuple[OperationType, ...]) -> list[Operation]:
        return []

    async def claim(self, operation_id: UUID) -> Operation | None:
        op = await self.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Operation) -> Operation:
        return operation


class FakeServerRepo:
    def __init__(self, servers: list[CloudServer]) -> None:
        self.servers = {s.id: s for s in servers}
        self.saved: list[CloudServer] = []

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_requested(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.REQUESTED]

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        return SPEC

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class FakeProvider:
    key = "hetzner"
    capabilities = frozenset({Capability.COMPUTE})

    def __init__(self) -> None:
        self.created: list[CreateServerRequest] = []
        self.active = 0
        self.max_active = 0

    async def list_images(self) -> list[ProviderImage]:
        return [ProviderImage(id="img-1", name="debian-12", os_family="linux", architecture="x86")]

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: Any
    ) -> ProviderServer:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.created.append(request)
            return ProviderServer(id="srv-1", name=request.name, status="creating")
        finally:
            self.active -= 1


def _make(
    servers: list[CloudServer],
    *,
    concurrency_limit: int = 3,
    op_repo: FakeOpRepo | None = None,
) -> tuple[ProvisioningWorker, FakeOpRepo, FakeProvider, FakeServerRepo]:
    repo = op_repo or FakeOpRepo()
    server_repo = FakeServerRepo(servers)
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)  # type: ignore[arg-type]
    worker = ProvisioningWorker(
        operation_repo=repo,  # type: ignore[arg-type]
        server_repo=server_repo,  # type: ignore[arg-type]
        provider_registry=registry,
        image_selector=FirstLinuxImageSelector(),
        wallet_repo=AsyncMock(),  # type: ignore[arg-type]
        hold_repo=AsyncMock(),  # type: ignore[arg-type]
        audit_repo=AsyncMock(),  # type: ignore[arg-type]
        concurrency_limit=concurrency_limit,
    )
    return worker, repo, provider, server_repo


class TestConcurrencyLimit:
    async def test_account_at_limit_is_skipped(self) -> None:
        # Account A already has 3 in-flight creates (the limit); a new
        # REQUESTED server for A must wait, while account B proceeds.
        busy1, busy2, busy3 = _busy_server(uuid4()), _busy_server(uuid4()), _busy_server(uuid4())
        new_a = _server(uuid4())
        new_b = _server(uuid4(), account=ACCOUNT_B)
        repo = FakeOpRepo()
        for busy in (busy1, busy2, busy3):
            repo.seed_in_flight(busy, f"server-create:{busy.id}")
        worker, _, provider, _ = _make(
            [busy1, busy2, busy3, new_a, new_b], concurrency_limit=3, op_repo=repo
        )

        counts = await worker.run_once()

        assert counts[ProvisioningOutcome.PROVISIONED] == 1  # only new_b
        assert counts[ProvisioningOutcome.SKIPPED_RATE_LIMITED] == 1  # new_a
        assert len(provider.created) == 1

    async def test_no_remaining_slots_skips_all(self) -> None:
        # Limit 1, account A already has 1 in-flight -> zero remaining slots.
        busy = _busy_server(uuid4())
        first = _server(uuid4())
        second = _server(uuid4())
        repo = FakeOpRepo()
        repo.seed_in_flight(busy, f"server-create:{busy.id}")
        worker, _, provider, _ = _make([busy, first, second], concurrency_limit=1, op_repo=repo)

        counts = await worker.run_once()

        assert counts.get(ProvisioningOutcome.PROVISIONED, 0) == 0
        assert counts[ProvisioningOutcome.SKIPPED_RATE_LIMITED] == 2
        assert provider.created == []

    async def test_sequential_run_never_exceeds_limit(self) -> None:
        # Limit 2, account A has 1 in-flight: the run may proceed (each call
        # adds at most 1 concurrent op), and total in-flight never exceeds 2.
        busy = _busy_server(uuid4())
        first = _server(uuid4())
        second = _server(uuid4())
        repo = FakeOpRepo()
        repo.seed_in_flight(busy, f"server-create:{busy.id}")
        worker, _, provider, _ = _make([busy, first, second], concurrency_limit=2, op_repo=repo)

        counts = await worker.run_once()

        assert counts[ProvisioningOutcome.PROVISIONED] == 2
        # At most one provider call from this run is in flight at any moment;
        # with the 1 pre-existing in-flight op, the account never exceeds 2.
        assert provider.max_active == 1
        assert 1 + provider.max_active <= 2

    async def test_accounts_are_limited_independently(self) -> None:
        # Limit 2; one in-flight per account -> each account still has room,
        # so both new servers proceed (the limits do not share a pool).
        busy_a = _busy_server(uuid4())
        busy_b = _busy_server(uuid4(), account=ACCOUNT_B)
        new_a = _server(uuid4())
        new_b = _server(uuid4(), account=ACCOUNT_B)
        repo = FakeOpRepo()
        repo.seed_in_flight(busy_a, f"server-create:{busy_a.id}")
        repo.seed_in_flight(busy_b, f"server-create:{busy_b.id}")
        worker, _, provider, _ = _make(
            [busy_a, busy_b, new_a, new_b], concurrency_limit=2, op_repo=repo
        )

        counts = await worker.run_once()

        assert counts[ProvisioningOutcome.PROVISIONED] == 2
        assert len(provider.created) == 2

    async def test_in_flight_op_for_deleted_row_does_not_count(self) -> None:
        # An in-flight op whose server row is gone (deletion) must not occupy
        # the account's slot.
        repo = FakeOpRepo()
        phantom_key = f"server-create:{uuid4()}"  # no server row for this
        op = Operation(
            id=uuid4(),
            operation_key=phantom_key,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=uuid4(),
            provider_key="hetzner",
            status=OperationStatus.IN_FLIGHT,
            attempts=1,
        )
        repo.in_flight.append(op)
        new_a = _server(uuid4())
        worker, _, provider, _ = _make([new_a], concurrency_limit=1, op_repo=repo)

        counts = await worker.run_once()

        assert counts[ProvisioningOutcome.PROVISIONED] == 1
        assert len(provider.created) == 1

    async def test_run_is_sequential_so_limit_one_processes_all(self) -> None:
        # The run processes servers one at a time (each op completes before
        # the next starts), so a limit of 1 still provisions every candidate.
        s1 = _server(uuid4())
        s2 = _server(uuid4())
        worker, _, provider, _ = _make([s1, s2], concurrency_limit=1)

        counts = await worker.run_once()

        assert counts[ProvisioningOutcome.PROVISIONED] == 2
        assert len(provider.created) == 2

    async def test_other_operation_types_do_not_count(self) -> None:
        # A power op in flight for the same account is not a create op.
        repo = FakeOpRepo()
        s = _server(uuid4(), state=ServerLifecycleState.RUNNING)
        op = Operation(
            id=uuid4(),
            operation_key=f"power_on:{s.id}:cmd",
            operation_type=OperationType.POWER_ON,
            resource_type="server",
            resource_id=s.id,
            provider_key="hetzner",
            status=OperationStatus.IN_FLIGHT,
            attempts=1,
        )
        repo.in_flight.append(op)
        new_a = _server(uuid4())
        worker, _, _, _ = _make([s, new_a], concurrency_limit=1, op_repo=repo)

        counts = await worker.run_once()

        assert counts[ProvisioningOutcome.PROVISIONED] == 1

    async def test_invalid_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="concurrency_limit"):
            _make([_server(uuid4())], concurrency_limit=0)

    async def test_default_limit_is_three(self) -> None:
        busy = [_busy_server(uuid4()) for _ in range(3)]
        new_a = _server(uuid4())
        repo = FakeOpRepo()
        for b in busy:
            repo.seed_in_flight(b, f"server-create:{b.id}")
        worker, _, provider, _ = _make([*busy, new_a], op_repo=repo)

        counts = await worker.run_once()

        assert counts[ProvisioningOutcome.SKIPPED_RATE_LIMITED] == 1
        assert provider.created == []

    async def test_limit_zero_candidates_short_circuits(self) -> None:
        repo = FakeOpRepo()
        worker, _, _, _ = _make([_server(uuid4())], op_repo=repo)

        assert await worker.run_once(limit=0) == {}
