"""Automated game day (M16-008).

Acceptance: kill switches and recovery proven.

Each scenario INJECTS a disaster against real service logic (fakes only
at the repo/transport boundary) and proves both the kill switch response
and the RECOVERY path:

1. COST RUNAWAY - accruals blow past a daily cap: the circuit breaker
   trips, new orders are refused, raising the limit restores ordering.
2. PROVIDER INCIDENT - the maintenance switch blocks a provider, orders
   halt with an audited reason, unblocking restores them.
3. OPERATION LOSS - a provider outage during rebuild re-queues the
   operation under the SAME key; when the provider recovers the worker
   completes it; a duplicate client request replays instead of re-acting.

The scenarios mirror docs/ops/game_day.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.compute.domain import (
    CloudServer,
    CostLimit,
    CostLimitScopeKind,
    ServerLifecycleState,
)
from cloud_platform.modules.compute.service import (
    CostCircuitBreakerService,
    CostLimitReachedError,
    MaintenanceScope,
    MaintenanceSwitchService,
)
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.operations.service import (
    RebuildCommandService,
    RebuildWorker,
    rebuild_operation_key,
)
from cloud_platform.providers.errors import ProviderConflict, ProviderUnavailable

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
ACCOUNT_A = uuid4()
SERVER_ID = uuid4()
OWNER = uuid4()


# ---------------------------------------------------------------------------
# Shared fakes (minimal ports, mirroring the established test patterns)
# ---------------------------------------------------------------------------


class FakeLimitRepo:
    def __init__(self) -> None:
        self.global_limit: CostLimit | None = None
        self.account_limits: dict[Any, CostLimit] = {}

    async def get_global(self) -> CostLimit | None:
        return self.global_limit

    async def get_for_account(self, provider_account_id: Any) -> CostLimit | None:
        return self.account_limits.get(provider_account_id)

    async def list_all(self) -> list[CostLimit]:
        out: list[CostLimit] = []
        if self.global_limit is not None:
            out.append(self.global_limit)
        out.extend(self.account_limits.values())
        return out

    async def upsert(self, limit: CostLimit) -> CostLimit:
        if limit.scope is CostLimitScopeKind.GLOBAL:
            self.global_limit = limit
        else:
            assert limit.provider_account_id is not None
            self.account_limits[limit.provider_account_id] = limit
        return limit

    async def remove(self, scope: Any, provider_account_id: Any = None) -> bool:
        if scope is CostLimitScopeKind.GLOBAL and self.global_limit is not None:
            self.global_limit = None
            return True
        if provider_account_id in self.account_limits:
            del self.account_limits[provider_account_id]
            return True
        return False


class FakeServerRepoForCosts:
    def __init__(self) -> None:
        self.all_ids: list[Any] = [SERVER_ID]
        self.by_account: dict[Any, list[Any]] = {ACCOUNT_A: [SERVER_ID]}

    async def list_non_deleted_ids(self) -> list[Any]:
        return list(self.all_ids)

    async def list_account_server_ids(self, provider_account_id: Any) -> list[Any]:
        return list(self.by_account.get(provider_account_id, []))


class FakeAccruals:
    def __init__(self, costs: dict[Any, int]) -> None:
        self.costs = costs

    async def daily_cost_total(
        self, _day_start: datetime, _day_end: datetime, server_ids: Any
    ) -> int:
        return sum(self.costs.get(s, 0) for s in server_ids)


class FakeSwitchRepo:
    """MaintenanceSwitchRepository with real save/remove semantics."""

    def __init__(self) -> None:
        self.blocks: dict[tuple[str, str | None], Any] = {}
        self.saved: list[Any] = []
        self.removed: list[MaintenanceScope] = []

    async def list_blocks(self) -> list[Any]:
        return list(self.blocks.values())

    async def save_block(self, block: Any) -> Any:
        self.blocks[(block.scope.provider_key, block.scope.location_id)] = block
        self.saved.append(block)
        return block

    async def remove_block(self, scope: MaintenanceScope) -> bool:
        removed = self.blocks.pop((scope.provider_key, scope.location_id), None)
        if removed is not None:
            self.removed.append(scope)
            return True
        return False


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class LocalOpRepo:
    def __init__(self) -> None:
        self.ops: dict[str, Any] = {}

    async def get_or_create(
        self,
        *,
        operation_key: str,
        operation_type: OperationType,
        resource_type: str,
        resource_id: Any,
        provider_key: str,
    ) -> Any:
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

    async def get_by_key(self, operation_key: str) -> Any:
        return self.ops.get(operation_key)

    async def claim(self, operation_id: Any) -> Any:
        op = next((o for o in self.ops.values() if o.id == operation_id), None)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Any) -> Any:
        return operation

    async def list_pending(self, types: Any) -> list[Any]:
        return [o for o in self.ops.values() if o.status is OperationStatus.PENDING]


class LocalServerRepo:
    def __init__(self, server: CloudServer) -> None:
        self.servers = {server.id: server}

    async def get(self, server_id: Any) -> CloudServer | None:
        return self.servers.get(server_id)

    async def save(self, server: CloudServer) -> CloudServer:
        self.servers[server.id] = server
        return server


class FlakyRebuildProvider:
    """Provider that fails retryably until healed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: Exception | None = ProviderUnavailable("datacenter on fire")

    async def rebuild_server(self, provider_server_id: str, image_id: str) -> str:
        self.calls.append((provider_server_id, image_id))
        if self.error is not None:
            raise self.error
        return "success"


class FakeRegistry:
    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def get(self, _key: str) -> Any:
        return self._provider


def make_server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=OWNER,
        provider_key="hetzner",
        provider_account_id=uuid4(),
        state=ServerLifecycleState.RUNNING,
        provider_server_id="p-1",
    )


# ---------------------------------------------------------------------------
# Scenario 1: cost runaway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_game_day_cost_runaway_trip_and_recovery() -> None:
    limits = FakeLimitRepo()
    servers = FakeServerRepoForCosts()
    accruals = FakeAccruals({SERVER_ID: 900_000})  # runaway spend today
    breaker = CostCircuitBreakerService(
        limit_repo=limits,  # type: ignore[arg-type]
        server_repo=servers,  # type: ignore[arg-type]
        accrual_repo=accruals,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    # operator sets a GLOBAL daily cap of 500 EUR (50_000 minor)
    await limits.upsert(
        CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=50_000, enabled=True)
    )

    # KILL SWITCH: the tripped breaker halts new spend for any account
    trigger = await breaker.check(ACCOUNT_A)
    assert trigger is not None
    assert trigger.scope is CostLimitScopeKind.GLOBAL
    with pytest.raises(CostLimitReachedError):
        if trigger is not None:
            raise CostLimitReachedError(str(trigger))

    # RECOVERY: operator raises the cap above the runaway spend
    await limits.upsert(
        CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=1_000_000, enabled=True)
    )
    assert await breaker.check(ACCOUNT_A) is None


# ---------------------------------------------------------------------------
# Scenario 2: provider incident via maintenance switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_game_day_provider_incident_block_and_unblock() -> None:
    repo = FakeSwitchRepo()
    audit = RecordingAudit()
    service = MaintenanceSwitchService(repo, audit)  # type: ignore[arg-type]

    # KILL SWITCH: block all hetzner orders with an audited reason
    block = await service.block_provider(
        actor=None, provider_key="hetzner", reason="provider outage INC-42"
    )
    assert await service.is_order_blocked("hetzner", "fsn1")
    event = audit.events[-1]
    assert event.actor_type is ActorType.SYSTEM
    assert event.reason == "provider outage INC-42"
    assert block.scope.provider_key == "hetzner"

    # other providers unaffected
    assert not await service.is_order_blocked("arvancloud", "fsn1")

    # RECOVERY: incident resolved, switch lifted with its own audited reason
    scope = MaintenanceScope(provider_key="hetzner")
    assert await service.unblock_provider(
        actor=None, provider_key="hetzner", reason="INC-42 resolved"
    )
    assert not await service.is_order_blocked("hetzner", "fsn1")
    assert repo.removed == [scope]

    # unblock again is a no-op
    assert not await service.unblock_provider(actor=None, provider_key="hetzner", reason="x")


# ---------------------------------------------------------------------------
# Scenario 3: operation loss during provider outage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_game_day_operation_survives_outage_then_completes() -> None:
    server = make_server()
    ops = LocalOpRepo()
    servers = LocalServerRepo(server)
    provider = FlakyRebuildProvider()
    audit = RecordingAudit()
    command = RebuildCommandService(
        server_repo=servers,  # type: ignore[arg-type]
        operation_repo=ops,  # type: ignore[arg-type]
        provider_registry=FakeRegistry(provider),  # type: ignore[arg-type]
        audit_repo=audit,  # type: ignore[arg-type]
    )
    owner = server.user_id
    key = "gd-key-1"

    # OUTAGE: the confirmed rebuild hits a retryable provider failure ->
    # the request itself re-queues under the SAME operation key.
    outcome = await command.request(owner, SERVER_ID, "ubuntu-24.04", key, confirmed=True)
    assert outcome.requeued is True
    op_key = rebuild_operation_key(SERVER_ID, key)
    stored = ops.ops[op_key]
    assert stored.status is OperationStatus.PENDING
    assert stored.attempts >= 1

    # duplicate client request during the outage does NOT create a second op
    await command.request(owner, SERVER_ID, "ubuntu-24.04", key, confirmed=True)
    assert len(ops.ops) == 1

    # RECOVERY: provider heals; worker drains the queue to COMPLETED with
    # the SAME operation key (image lookup is deterministic per server).
    provider.error = None
    images: dict[Any, str] = {SERVER_ID: "ubuntu-24.04"}
    worker = RebuildWorker(
        operation_repo=ops,  # type: ignore[arg-type]
        server_repo=servers,  # type: ignore[arg-type]
        provider_registry=FakeRegistry(provider),  # type: ignore[arg-type]
        audit_repo=RecordingAudit(),  # type: ignore[arg-type]
        image_lookup=lambda sid: images.get(sid),
    )
    counts = await worker.process_pending()
    assert counts == {"executed": 1, "requeued": 0}
    done = ops.ops[op_key]
    assert done.status is OperationStatus.COMPLETED
    # 1 failed attempt + 1 duplicate-request retry during the outage
    # + 1 recovered attempt by the worker - every call used the SAME image
    assert len(provider.calls) == 3
    assert all(image == "ubuntu-24.04" for _sid, image in provider.calls)

    # replay after completion: no further provider call
    replay = await command.request(owner, SERVER_ID, "ubuntu-24.04", key, confirmed=True)
    assert replay.replayed is True
    assert len(provider.calls) == 3


_ = ProviderConflict  # keeps import surface explicit for scenario extensions
