"""Tests for the provider action waiter strategy (M07-004).

Acceptance: polls with bounded backoff and rate-limit awareness.
"""

from __future__ import annotations

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
    ProvisioningWorker,
)
from cloud_platform.modules.wallet.domain import Wallet
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    ProviderImage,
    ProviderServer,
)
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.retry import RetryExecutor, RetryPolicy
from cloud_platform.providers.waiter import (
    ActionWaiter,
    WaitOutcome,
    WaitPolicy,
    WaitProbe,
    WaitState,
)

SERVER_ID = uuid4()
USER_ID = uuid4()
WALLET_ID = uuid4()
SPEC = ProvisioningSpec(plan_id="cx22", location_id="fsn1", currency="EUR")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _waiter(
    policy: WaitPolicy, clock: FakeClock, retry: RetryExecutor | None = None
) -> ActionWaiter:
    return ActionWaiter(policy, sleep_fn=clock.sleep, clock_fn=clock, retry_executor=retry)


async def _probe(state: WaitState, detail: str | None = None) -> WaitProbe:
    return WaitProbe(state, detail)


class TestWaitPolicy:
    def test_defaults_are_bounded(self) -> None:
        policy = WaitPolicy()
        assert policy.max_wait_seconds > policy.max_delay_seconds > policy.base_delay_seconds

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"max_wait_seconds": 0},
            {"base_delay_seconds": 0},
            {"max_delay_seconds": 1.0, "base_delay_seconds": 2.0},
            {"multiplier": 0.5},
        ],
    )
    def test_invalid_policy_rejected(self, kwargs: dict) -> None:
        with pytest.raises(ValueError):
            WaitPolicy(**kwargs)


class TestWaitFor:
    async def test_immediate_completion(self) -> None:
        clock = FakeClock()
        waiter = _waiter(WaitPolicy(), clock)
        result = await waiter.wait_for(lambda: _probe(WaitState.COMPLETED))
        assert result.outcome is WaitOutcome.COMPLETED
        assert result.polls == 1
        assert result.elapsed_seconds == 0.0

    async def test_completes_after_pending_polls(self) -> None:
        clock = FakeClock()
        waiter = _waiter(WaitPolicy(max_wait_seconds=100, base_delay_seconds=2.0), clock)
        states = [WaitState.PENDING, WaitState.PENDING, WaitState.COMPLETED]

        async def next_probe() -> WaitProbe:
            return WaitProbe(states.pop(0))

        result = await waiter.wait_for(next_probe)
        assert result.outcome is WaitOutcome.COMPLETED
        assert result.polls == 3
        assert result.elapsed_seconds == pytest.approx(2.0 + 4.0)  # backoff applied

    async def test_failed_probe_returns_failure_with_detail(self) -> None:
        clock = FakeClock()
        waiter = _waiter(WaitPolicy(), clock)
        result = await waiter.wait_for(lambda: _probe(WaitState.FAILED, "boom"))
        assert result.outcome is WaitOutcome.FAILED
        assert result.detail == "boom"
        assert result.polls == 1

    async def test_deadline_returns_timeout(self) -> None:
        clock = FakeClock()
        waiter = _waiter(
            WaitPolicy(max_wait_seconds=5.0, base_delay_seconds=2.0, max_delay_seconds=3.0),
            clock,
        )
        result = await waiter.wait_for(lambda: _probe(WaitState.PENDING))
        assert result.outcome is WaitOutcome.TIMEOUT
        assert result.elapsed_seconds <= 5.0
        # polls: t=0 pending, sleep 2, t=2 pending, sleep 3, t=5 -> deadline
        assert result.polls == 3

    async def test_backoff_is_bounded(self) -> None:
        clock = FakeClock()
        policy = WaitPolicy(
            max_wait_seconds=10_000.0, base_delay_seconds=2.0, max_delay_seconds=5.0
        )
        delays: list[float] = []

        async def spy_sleep(seconds: float) -> None:
            delays.append(seconds)
            clock.now += seconds

        waiter = ActionWaiter(policy, sleep_fn=spy_sleep, clock_fn=clock)
        await waiter.wait_for(lambda: _probe(WaitState.PENDING))
        assert all(d <= 5.0 for d in delays)
        assert delays[:4] == [2.0, 4.0, 5.0, 5.0]  # exponential, then clamped

    async def test_transient_error_retried_within_poll(self) -> None:
        clock = FakeClock()
        waiter = _waiter(WaitPolicy(max_wait_seconds=100), clock)
        calls = 0

        async def flaky() -> WaitProbe:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderUnavailable("502")
            return WaitProbe(WaitState.COMPLETED)

        result = await waiter.wait_for(flaky)
        assert result.outcome is WaitOutcome.COMPLETED
        assert result.polls == 1  # the 502 was absorbed by the per-poll retry
        assert calls == 2

    async def test_rate_limit_reset_honored(self) -> None:
        clock = FakeClock()
        retry = RetryExecutor(
            RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=60.0, jitter=0.0),
            sleep_fn=clock.sleep,
            clock_fn=clock,
        )
        waiter = _waiter(WaitPolicy(max_wait_seconds=100), clock, retry)
        calls = 0

        async def limited() -> WaitProbe:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ProviderRateLimited("429", reset_at_unix=clock.now + 5.0)
            return WaitProbe(WaitState.COMPLETED)

        result = await waiter.wait_for(limited)
        assert result.outcome is WaitOutcome.COMPLETED
        assert clock.now >= 1005.0  # the retry slept out the reset window

    async def test_retry_budget_exhaustion_propagates(self) -> None:
        clock = FakeClock()
        retry = RetryExecutor(
            RetryPolicy(max_attempts=2, base_delay_seconds=0.1, max_delay_seconds=0.2, jitter=0.0),
            sleep_fn=clock.sleep,
            clock_fn=clock,
        )
        waiter = _waiter(WaitPolicy(max_wait_seconds=100), clock, retry)

        async def always_down() -> WaitProbe:
            raise ProviderUnavailable("503")

        with pytest.raises(ProviderUnavailable):
            await waiter.wait_for(always_down)

    async def test_permanent_error_propagates_immediately(self) -> None:
        clock = FakeClock()
        waiter = _waiter(WaitPolicy(max_wait_seconds=100), clock)

        async def bad_auth() -> WaitProbe:
            raise ProviderAuthError("401")

        with pytest.raises(ProviderAuthError):
            await waiter.wait_for(bad_auth)


# ---------------------------------------------------------------------------
# Integration: ProvisioningWorker uses the waiter to fast-forward state
# ---------------------------------------------------------------------------


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

    async def list_in_flight(self, operation_type: OperationType) -> list[Operation]:
        return []


class FakeServerRepo:
    def __init__(self, servers: dict[UUID, CloudServer]) -> None:
        self.servers = dict(servers)

    async def get(self, server_id: UUID) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_requested(self) -> list[CloudServer]:
        return []

    async def get_provisioning_spec(self, server_id: UUID) -> ProvisioningSpec | None:
        return SPEC

    async def save(self, server: CloudServer) -> CloudServer:
        return server


class StatusProvider:
    """Provider double: create returns a server; get_server walks statuses."""

    key = "hetzner"
    capabilities = frozenset({Capability.COMPUTE})

    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.get_calls = 0
        self.create_calls = 0
        self.images = [
            ProviderImage(id="img-linux", name="debian", os_family="linux", architecture="x86")
        ]

    async def list_images(self) -> list[ProviderImage]:
        return self.images

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: object
    ) -> ProviderServer:
        self.create_calls += 1
        return ProviderServer(id="prov-123", name="srv-x", status="creating")

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        self.get_calls += 1
        index = min(self.get_calls - 1, len(self.statuses) - 1)
        return ProviderServer(id=provider_server_id, name="srv-x", status=self.statuses[index])


def _server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.REQUESTED,
        idempotency_key="cmd-key-1",
    )


def _worker_with_waiter(
    statuses: list[str], waiter: ActionWaiter | None
) -> tuple[FakeOpRepo, FakeServerRepo, StatusProvider, ProvisioningWorker, CloudServer]:
    server = _server()
    provider = StatusProvider(statuses)
    registry = ProviderRegistry()
    registry.register(provider)  # type: ignore[arg-type]
    wallets = AsyncMock()
    wallets.get = AsyncMock(
        return_value=Wallet(user_id=USER_ID, id=WALLET_ID, balance=100, currency="EUR")
    )
    holds = AsyncMock()
    holds.get_by_idempotency = AsyncMock(return_value=None)
    holds.release_hold = AsyncMock(return_value=None)
    audit = AsyncMock()
    audit.append = AsyncMock(side_effect=lambda e: e)
    op_repo = FakeOpRepo()
    server_repo = FakeServerRepo({server.id: server})
    worker = ProvisioningWorker(
        operation_repo=op_repo,  # type: ignore[arg-type]
        server_repo=server_repo,  # type: ignore[arg-type]
        provider_registry=registry,
        image_selector=FirstLinuxImageSelector(),
        wallet_repo=wallets,  # type: ignore[arg-type]
        hold_repo=holds,  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
        waiter=waiter,
    )
    return op_repo, server_repo, provider, worker, audit


class TestWorkerWaiterIntegration:
    async def test_wait_fast_forwards_to_running(self) -> None:
        clock = FakeClock()
        waiter = ActionWaiter(
            WaitPolicy(max_wait_seconds=60, base_delay_seconds=1.0),
            sleep_fn=clock.sleep,
            clock_fn=clock,
        )
        _, server_repo, provider, worker, audit = _worker_with_waiter(
            ["initializing", "running"], waiter
        )

        outcome = await worker.process_server(SERVER_ID)

        assert outcome.value == "provisioned"
        saved = await server_repo.get(SERVER_ID)
        assert saved is not None
        assert saved.state.value == "running"
        assert provider.get_calls == 2
        actions = [c.args[0].action for c in audit.append.call_args_list]
        assert "server.provisioning_wait_completed" in actions

    async def test_timeout_keeps_provisioning(self) -> None:
        clock = FakeClock()
        waiter = ActionWaiter(
            WaitPolicy(max_wait_seconds=2.0, base_delay_seconds=1.0),
            sleep_fn=clock.sleep,
            clock_fn=clock,
        )
        _, server_repo, _, worker, audit = _worker_with_waiter(["initializing"], waiter)

        outcome = await worker.process_server(SERVER_ID)

        assert outcome.value == "provisioned"
        saved = await server_repo.get(SERVER_ID)
        assert saved is not None
        assert saved.state.value == "provisioning"
        actions = [c.args[0].action for c in audit.append.call_args_list]
        assert "server.provisioning_wait_timeout" in actions

    async def test_provider_error_during_watch_is_contained(self) -> None:
        clock = FakeClock()
        waiter = ActionWaiter(
            WaitPolicy(max_wait_seconds=60, base_delay_seconds=1.0),
            sleep_fn=clock.sleep,
            clock_fn=clock,
        )
        _, server_repo, provider, worker, _ = _worker_with_waiter(["initializing"], waiter)

        async def broken_get(provider_server_id: str) -> ProviderServer | None:
            raise ProviderUnavailable("connection reset")

        provider.get_server = broken_get  # type: ignore[method-assign]

        outcome = await worker.process_server(SERVER_ID)

        # the watch is non-fatal: provisioning continues, reconciler owns it
        assert outcome.value == "provisioned"
        saved = await server_repo.get(SERVER_ID)
        assert saved is not None
        assert saved.state.value == "provisioning"

    async def test_no_waiter_keeps_legacy_behavior(self) -> None:
        _, server_repo, provider, worker, _ = _worker_with_waiter(["initializing"], None)

        outcome = await worker.process_server(SERVER_ID)

        assert outcome.value == "provisioned"
        saved = await server_repo.get(SERVER_ID)
        assert saved is not None
        assert saved.state.value == "provisioning"
        assert provider.get_calls == 0  # no waiting at all
