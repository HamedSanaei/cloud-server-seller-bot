"""Tests for the alert-feed metrics wiring (M11-006).

Acceptance (metrics half): the five alert topics have their feeds wired at
the call sites - spend gauges set by the cost circuit breaker, the
provisioning-failure counter incremented at each permanent-failure stage,
the reconciliation-outcome counter incremented for every reconcile()
outcome, and the operation queue-age gauge set by the worker jobs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry

from cloud_platform.modules.compute.domain import CostLimit, CostLimitScopeKind
from cloud_platform.modules.compute.service import CostCircuitBreakerService
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.observability.metrics import PlatformMetrics
from cloud_platform.providers.errors import ProviderRateLimited

NOW = datetime(2026, 8, 24, 15, 0, tzinfo=UTC)
ACCOUNT_A = uuid4()
SERVER_A1 = uuid4()
SERVER_A2 = uuid4()


def _sample(registry: CollectorRegistry, name: str, **labels: str) -> float:
    """Look a sample up by (name, labelset) in the registry."""
    for metric in registry.collect():
        for s in metric.samples:
            if s.name == name and s.labels == labels:
                return float(s.value)
    raise AssertionError(f"metric {name}{labels} not found in the registry")


class FakeLimitRepo:
    def __init__(self, global_limit: CostLimit | None = None) -> None:
        self._global = global_limit
        self._account: dict[UUID, CostLimit] = {}

    async def get_global(self) -> CostLimit | None:
        return self._global

    async def get_for_account(self, provider_account_id: UUID) -> CostLimit | None:
        return self._account.get(provider_account_id)

    async def list_all(self) -> list[CostLimit]:
        return ([self._global] if self._global is not None else []) + list(self._account.values())

    async def upsert(self, limit: CostLimit) -> CostLimit:
        if limit.scope is CostLimitScopeKind.GLOBAL:
            self._global = limit
        else:
            assert limit.provider_account_id is not None
            self._account[limit.provider_account_id] = limit
        return limit

    async def remove(
        self, scope: CostLimitScopeKind, provider_account_id: UUID | None = None
    ) -> bool:
        return False


class FakeServerRepo:
    def __init__(self, ids: list[UUID]) -> None:
        self._ids = ids

    async def list_non_deleted_ids(self) -> list[UUID]:
        return list(self._ids)

    async def list_account_server_ids(self, provider_account_id: UUID) -> list[UUID]:
        return list(self._ids) if provider_account_id == ACCOUNT_A else []


class FakeAccrualRepo:
    def __init__(self, cost: int) -> None:
        self._cost = cost

    async def daily_cost_total(
        self, day_start: datetime, day_end: datetime, server_ids: frozenset[UUID]
    ) -> int:
        return self._cost if server_ids else 0


class TestSpendFeed:
    async def test_check_sets_the_global_daily_cost_gauge(self) -> None:
        registry = CollectorRegistry()
        m = PlatformMetrics(registry=registry)
        limits = FakeLimitRepo(
            global_limit=CostLimit(scope=CostLimitScopeKind.GLOBAL, limit_minor=10_000)
        )
        breaker = CostCircuitBreakerService(
            limit_repo=limits,
            server_repo=FakeServerRepo([SERVER_A1, SERVER_A2]),  # type: ignore[arg-type]
            accrual_repo=FakeAccrualRepo(12345),
            clock=lambda: NOW,
        )
        # patch the module-level singleton the service records through
        from cloud_platform.modules import compute

        original = compute.service.metrics
        compute.service.metrics = m
        try:
            trigger = await breaker.check(ACCOUNT_A)
        finally:
            compute.service.metrics = original

        # spend 12345 >= limit 10000 -> the breaker trips (the gate works)
        assert trigger is not None
        assert trigger.scope is CostLimitScopeKind.GLOBAL
        assert trigger.spent_minor == 12345
        # ...and the spend gauge carries the same number (the alert feed)
        assert _sample(registry, "cloud_platform_daily_global_provider_cost_minor") == 12345.0

    async def test_no_global_limit_still_sets_the_gauge(self) -> None:
        """The spend gauge is the day's cost regardless of configured limits
        (the spend spike alert must work with no breaker configured)."""
        registry = CollectorRegistry()
        m = PlatformMetrics(registry=registry)
        breaker = CostCircuitBreakerService(
            limit_repo=FakeLimitRepo(),
            server_repo=FakeServerRepo([SERVER_A1]),  # type: ignore[arg-type]
            accrual_repo=FakeAccrualRepo(5),
            clock=lambda: NOW,
        )
        from cloud_platform.modules import compute

        original = compute.service.metrics
        compute.service.metrics = m
        try:
            assert await breaker.check(ACCOUNT_A) is None
        finally:
            compute.service.metrics = original

        assert _sample(registry, "cloud_platform_daily_global_provider_cost_minor") == 5.0


class TestProvisioningFailureFeed:
    async def test_worker_failure_stage_counts(self) -> None:
        """process_server -> _fail_permanent (the worker) increments stage=worker."""
        from cloud_platform.modules.compute.domain import (
            CloudServer,
            ServerLifecycleState,
        )
        from cloud_platform.modules.operations.service import ProvisioningWorker

        registry = CollectorRegistry()
        m = PlatformMetrics(registry=registry)

        server_id = uuid4()
        user_id = uuid4()

        class _ServerRepo:
            def __init__(self) -> None:
                self._server = CloudServer(
                    id=server_id,
                    user_id=user_id,
                    provider_key="hetzner",
                    provider_account_id=ACCOUNT_A,
                    state=ServerLifecycleState.PROVISIONING,
                    idempotency_key="ik",
                    provider_server_id="prov-1",
                )

            async def get(self, server_id_):
                return self._server

            async def save(self, s):
                return s

            async def get_provisioning_spec(self, server_id_):
                # None -> permanent failure "catalog offer missing for server"
                return None

        ops: dict[str, Operation] = {}

        class _OpRepo:
            async def get_or_create(
                self, *, operation_key, operation_type, resource_type, resource_id, provider_key
            ):
                op = ops.get(operation_key)
                if op is None:
                    op = Operation(
                        id=uuid4(),
                        operation_key=operation_key,
                        operation_type=operation_type,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        provider_key=provider_key,
                    )
                    ops[operation_key] = op
                return op

            async def get_by_key(self, key):
                return ops.get(key)

            async def claim(self, operation_id):
                for op in ops.values():
                    if op.id == operation_id and op.status is OperationStatus.PENDING:
                        op.mark_in_flight()
                        return op
                return None

            async def save(self, operation):
                ops[operation.operation_key] = operation
                return operation

        worker = ProvisioningWorker(  # type: ignore[call-arg]
            operation_repo=_OpRepo(),
            server_repo=_ServerRepo(),
            provider_registry=MagicMock(),  # type: ignore[arg-type]
            image_selector=MagicMock(),  # type: ignore[arg-type]
            audit_repo=AsyncMock(),
            wallet_repo=AsyncMock(),
            hold_repo=AsyncMock(),
        )

        from cloud_platform.modules import operations

        original = operations.service.metrics
        operations.service.metrics = m
        try:
            outcome = await worker.process_server(server_id)
        finally:
            operations.service.metrics = original

        assert outcome.name == "FAILED"
        assert (
            _sample(registry, "cloud_platform_provisioning_failures_total", stage="worker") == 1.0
        )


class TestReconciliationFeed:
    async def test_every_outcome_is_counted(self) -> None:
        """reconcile() counts each outcome into the drift feed."""
        from cloud_platform.modules.operations.service import (
            CreateTimeoutReconciler,
        )

        registry = CollectorRegistry()
        m = PlatformMetrics(registry=registry)

        class _ServerRepo:
            async def list_requested(self):
                return []

            async def list_provisioning(self):
                return []

            async def get(self, server_id):
                return None

            async def save(self, s):
                return s

        class _OpRepo:
            def __init__(self) -> None:
                self._op = None

            async def list_in_flight(self, operation_type):
                # one timed-out in-flight op
                op = Operation(
                    id=uuid4(),
                    operation_key="server-create:x",
                    operation_type=OperationType.SERVER_CREATE,
                    resource_type="server",
                    resource_id=uuid4(),
                    provider_key="hetzner",
                )
                op.mark_in_flight()
                op.updated_at = NOW - timedelta(hours=2)
                self._op = op
                return [op]

            async def get_by_key(self, key):
                return None

            async def get(self, operation_id):
                return self._op

            async def get_or_create(self, **kw):
                raise AssertionError("no recreations in this test")

            async def save(self, operation):
                return operation

        class _Registry:
            def get(self, key):
                raise KeyError(key)  # unknown provider -> contained

        class _ImageSelector:
            async def select(self, *a, **k):
                return None

        reconciler = CreateTimeoutReconciler(  # type: ignore[call-arg]
            operation_repo=_OpRepo(),
            server_repo=_ServerRepo(),
            provider_registry=_Registry(),  # type: ignore[arg-type]
            image_selector=_ImageSelector(),
            wallet_repo=AsyncMock(),
            hold_repo=AsyncMock(),
            audit_repo=AsyncMock(),
            clock=lambda: NOW,
        )

        from cloud_platform.modules import operations

        original = operations.service.metrics
        operations.service.metrics = m
        try:
            counts = await reconciler.reconcile()
        finally:
            operations.service.metrics = original

        # every counted outcome is in the feed
        total = sum(counts.values())
        assert total >= 1
        feed_total = 0.0
        for metric in registry.collect():
            for s in metric.samples:
                if s.name == "cloud_platform_reconciliation_outcomes_total":
                    assert s.labels.get("reconciler") == "create"
                    feed_total += float(s.value)
        assert feed_total == float(total)


class TestProviderCallOutcomeFeed:
    """The 429 alert rides on provider_calls_total{outcome="rate_limited"}:
    the provider_call context manager must map ProviderRateLimited to the
    closed label."""

    async def test_rate_limited_maps_to_closed_label(self) -> None:
        registry = CollectorRegistry()
        m = PlatformMetrics(registry=registry)
        with pytest.raises(ProviderRateLimited):
            async with m.provider_call("hetzner", "get_server"):
                raise ProviderRateLimited("throttled")
        assert (
            _sample(
                registry,
                "cloud_platform_provider_calls_total",
                provider="hetzner",
                operation="get_server",
                outcome="rate_limited",
            )
            == 1.0
        )
