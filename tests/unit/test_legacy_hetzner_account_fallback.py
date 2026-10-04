"""Legacy pool behavior with isolated durable-receipt doubles, not PostgreSQL proof."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    ServerLifecycleState,
)
from cloud_platform.modules.operations.create_attempts import (
    CreateAttemptConflict,
    create_routing,
    last_attempt,
    safe_pre_post,
)
from cloud_platform.modules.operations.domain import Operation, OperationStatus, OperationType
from cloud_platform.modules.operations.service import (
    CreateTimeoutReconciler,
    ProvisioningOutcome,
    ProvisioningWorker,
    ReconciliationOutcome,
)
from cloud_platform.modules.provider_routes.domain import ProviderRoute, RouteState
from cloud_platform.modules.provider_routes.service import ProviderRouteSelector
from cloud_platform.providers.base import (
    AccountServerUsage,
    OrderRecoveryResult,
    OrderRecoveryVerdict,
    ProviderLocation,
    ProviderPlan,
    ProviderServer,
)
from cloud_platform.providers.errors import (
    ProviderCapacityError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
from cloud_platform.providers.registry import ProviderRegistry
from tests.unit.test_provisioning_worker import OP_KEY, FakeProvider, _Deps, _hold, _server

NOW = datetime(2026, 10, 3, tzinfo=UTC)
STALE = NOW - timedelta(hours=2)


class ReceiptStore:
    """Fenced snapshots model the service boundary; real row locks are tested separately."""

    def __init__(self, deps: _Deps) -> None:
        self.deps = deps
        self.fail_refusal = False
        self.before_sent: Any = None

    async def _live(self, operation_id: Any, generation: int) -> Operation:
        op = await self.deps.ops.get(operation_id)
        if (
            op is None
            or op.attempts != generation
            or op.status
            not in (
                OperationStatus.IN_FLIGHT,
                OperationStatus.OUTCOME_UNKNOWN,
            )
        ):
            raise CreateAttemptConflict("stale generation")
        return op

    async def claim(self, operation_id: Any, server_id: Any, **kwargs: Any) -> Operation | None:
        op = await self.deps.ops.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        receipt = create_routing(op)
        if receipt is None:
            if op.attempts:
                raise CreateAttemptConflict("historical ambiguous attempt")
            op.provider_response = {
                "create_routing": {
                    "version": 1,
                    "policy": "capacity_failover",
                    "attempts": [],
                    "catalog_account_id": kwargs.get("catalog_account_id"),
                }
            }
        elif not safe_pre_post(op):
            raise CreateAttemptConflict("no pre-POST proof")
        op.mark_in_flight()
        return deepcopy(op)

    async def start_attempt(
        self,
        operation_id: Any,
        generation: int,
        account: str,
        *,
        request_facts: Any = None,
    ) -> Operation:
        if self.before_sent is not None:
            await self.before_sent()
        op = await self._live(operation_id, generation)
        if not safe_pre_post(op):
            raise CreateAttemptConflict("unsafe POST")
        server = await self.deps.server_repo.get(op.resource_id)
        assert server is not None
        fingerprint = dict(server.offer_fingerprint or {})
        frozen = fingerprint.get("legacy_create_request")
        if frozen is not None and frozen != request_facts:
            raise CreateAttemptConflict("changed request")
        fingerprint["legacy_create_request"] = dict(request_facts)
        server.offer_fingerprint = fingerprint
        server.image_id = request_facts["image_id"]
        server.credential_account_id = account
        receipt = op.provider_response["create_routing"]
        receipt["attempts"].append(
            {
                "sequence": len(receipt["attempts"]) + 1,
                "account_id": account,
                "phase": "sent",
            }
        )
        return deepcopy(op)

    async def record_refusal(
        self,
        operation_id: Any,
        generation: int,
        account: str,
        **facts: Any,
    ) -> Operation:
        if self.fail_refusal:
            raise RuntimeError("refusal acknowledgment lost")
        op = await self._live(operation_id, generation)
        attempt = op.provider_response["create_routing"]["attempts"][-1]
        assert attempt["account_id"] == account and attempt["phase"] == "sent"
        attempt["phase"] = "capacity_refused" if facts["capacity"] else "refused"
        if facts.get("error_code"):
            attempt["error_code"] = facts["error_code"]
        attempt["quota_names"] = list(facts.get("quota_names", ()))
        return deepcopy(op)

    async def record_unknown(self, operation_id: Any, generation: int, account: str) -> Operation:
        op = await self._live(operation_id, generation)
        op.provider_response["create_routing"]["attempts"][-1]["phase"] = "outcome_unknown"
        op.mark_outcome_unknown("uncertain")
        return deepcopy(op)

    async def record_acceptance(
        self,
        operation_id: Any,
        generation: int,
        account: str,
        identity: str,
        **kwargs: Any,
    ) -> Operation:
        op = await self._live(operation_id, generation)
        server = await self.deps.server_repo.get(op.resource_id)
        assert server is not None and server.credential_account_id == account
        receipt = op.provider_response["create_routing"]
        receipt["attempts"][-1].update(phase="accepted", provider_server_id=identity)
        receipt["accepted_account_id"] = account
        op.provider_response["provider_server_id"] = identity
        server.provider_server_id = identity
        return deepcopy(op)

    async def save_outcome(
        self,
        operation_id: Any,
        generation: int,
        status: OperationStatus,
        **facts: Any,
    ) -> Operation:
        op = await self._live(operation_id, generation)
        server = await self.deps.server_repo.get(op.resource_id)
        assert server is not None
        attempt = last_attempt(op)
        if status is OperationStatus.FAILED:
            assert attempt is None or attempt["phase"] in ("capacity_refused", "refused")
            server.state = ServerLifecycleState.ERROR
        if status is OperationStatus.COMPLETED:
            assert attempt is not None and attempt["phase"] == "accepted"
            server.state = ServerLifecycleState.PROVISIONING
        op.status = status
        op.error = facts.get("error")
        op.provider_response.update(facts.get("correlation", {}))
        return deepcopy(op)

    async def resume_safe_claim(self, operation_id: Any, generation: int) -> Operation:
        op = await self._live(operation_id, generation)
        op.attempts += 1
        if safe_pre_post(op):
            op.status = OperationStatus.PENDING
        elif last_attempt(op)["phase"] == "refused":
            op.status = OperationStatus.IN_FLIGHT
        elif last_attempt(op)["phase"] != "accepted":
            op.provider_response["create_routing"]["attempts"][-1]["phase"] = "outcome_unknown"
            op.status = OperationStatus.OUTCOME_UNKNOWN
        return deepcopy(op)


class AccountProvider(FakeProvider):
    key = "hetzner"

    def __init__(self, account: str) -> None:
        super().__init__()
        self.account = account
        self.requests: list[Any] = []
        self.recoveries: list[Any] = []
        self.recovery = OrderRecoveryResult(OrderRecoveryVerdict.NO_MATCH)
        self.on_post: Any = None

    async def list_locations(self) -> list[ProviderLocation]:
        return [ProviderLocation("1", "fsn1", "DE")]

    async def list_plans(self) -> list[ProviderPlan]:
        return [ProviderPlan("22", "cx22", "x86", 2, 4096, 40)]

    async def validate_create_request(self, request: Any) -> None:
        if request.location_id != "fsn1" or request.plan_id != "cx22":
            raise ProviderNotFound("original product/location unavailable")
        image = next((image for image in self.images if image.id == request.image_id), None)
        if image is None or image.architecture != "x86":
            raise ProviderNotFound("original image unavailable")

    async def create_server(self, request: Any, idempotency_key: Any) -> ProviderServer:
        self.requests.append(request)
        self.create_calls += 1
        if self.on_post is not None:
            await self.on_post()
        if self.error is not None:
            raise self.error
        return ProviderServer("101" if self.account == "a" else "202", request.name, "initializing")

    async def get_server(self, identity: str) -> ProviderServer:
        return ProviderServer(identity, "recovered", "initializing")

    async def recover_server_by_operation(self, key: str, **kwargs: Any) -> OrderRecoveryResult:
        self.recoveries.append((key, kwargs))
        return self.recovery


class Usage:
    def __init__(self) -> None:
        self.counts: dict[str, Any] = {"a": 4, "b": 2}

    def accepts_new_orders(self, account: str) -> bool:
        return True

    async def server_usage(self, account: str) -> AccountServerUsage:
        count = self.counts[account]
        if isinstance(count, Exception):
            raise count
        return AccountServerUsage(account, count, 5)


class Pool:
    def __init__(self) -> None:
        self.server = _server()
        self.server.provider_key = "hetzner"
        self.server.credential_account_id = "a"
        self.a = AccountProvider("a")
        self.b = AccountProvider("b")
        self.deps = _Deps({self.server.id: self.server}, self.a)
        self.deps.holds.get_by_idempotency = AsyncMock(return_value=_hold())
        self.deps.registry = ProviderRegistry()
        self.deps.registry.disable_default_account_fallback("hetzner")
        for provider in (self.a, self.b):
            self.deps.registry.register_route("hetzner", provider.account, provider)
        self.deps.ops.get_by_key = AsyncMock(side_effect=lambda key: self.deps.ops.ops.get(key))
        self.deps.ops.list_in_flight = AsyncMock(
            side_effect=lambda _: [
                op for op in self.deps.ops.ops.values() if op.status is OperationStatus.IN_FLIGHT
            ]
        )
        self.deps.server_repo.list_provisioning = AsyncMock(
            side_effect=lambda: [
                server
                for server in self.deps.server_repo.servers.values()
                if server.state is ServerLifecycleState.PROVISIONING
            ]
        )
        self.usage = Usage()
        self.routes = AsyncMock()
        self.routes.list_for_location = AsyncMock(
            return_value=[
                ProviderRoute(
                    "hetzner",
                    account,
                    "fsn1",
                    RouteState.ELIGIBLE_AVAILABLE,
                    priority=priority,
                    product_ids=("cx22",),
                )
                for priority, account in enumerate(("a", "b"))
            ]
        )
        self.receipts = ReceiptStore(self.deps)
        image_selector = self.deps.worker._images
        selector = ProviderRouteSelector(
            repository=self.routes,
            usage_readers={"hetzner": self.usage},
        )
        self.deps.worker = ProvisioningWorker(
            operation_repo=self.deps.ops,
            server_repo=self.deps.server_repo,
            provider_registry=self.deps.registry,
            image_selector=image_selector,
            wallet_repo=self.deps.wallets,
            hold_repo=self.deps.holds,
            audit_repo=self.deps.audit,
            create_attempts=self.receipts,
            fulfillment_routes=selector,
            prepay_server=AsyncMock(),
        )
        self.reconciler = CreateTimeoutReconciler(
            operation_repo=self.deps.ops,
            server_repo=self.deps.server_repo,
            provider_registry=self.deps.registry,
            image_selector=self.deps.worker._images,
            wallet_repo=self.deps.wallets,
            hold_repo=self.deps.holds,
            audit_repo=self.deps.audit,
            create_attempts=self.receipts,
            clock=lambda: NOW,
        )

    @property
    def operation(self) -> Operation:
        return self.deps.ops.ops[OP_KEY]

    async def process(self) -> ProvisioningOutcome:
        return await self.deps.worker.process_server(self.server.id)


async def test_full_preferred_project_uses_next_account_without_first_post() -> None:
    pool = Pool()
    pool.usage.counts["a"] = 5
    assert await pool.process() is ProvisioningOutcome.PROVISIONED
    assert pool.a.create_calls == 0 and pool.b.create_calls == 1
    assert pool.server.credential_account_id == "b" and pool.server.provider_server_id == "202"
    assert last_attempt(pool.operation)["phase"] == "accepted"
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_documented_refusal_commits_before_identical_request_on_next_account() -> None:
    pool = Pool()
    pool.a.error = ProviderCapacityError(
        "full",
        error_code="resource_limit_exceeded",
        quota_names=("project_limit",),
        definitive_refusal=True,
    )
    pool.b.images.reverse()

    async def before_second_post() -> None:
        receipt = create_routing(pool.operation)
        assert [attempt["phase"] for attempt in receipt["attempts"]] == ["capacity_refused", "sent"]
        assert pool.server.credential_account_id == "b"

    pool.b.on_post = before_second_post
    assert await pool.process() is ProvisioningOutcome.PROVISIONED
    assert pool.a.requests == pool.b.requests
    assert pool.server.offer_fingerprint["legacy_create_request"]["image_id"] == "img-linux"
    assert pool.operation.attempts == 1
    pool.deps.holds.release_hold.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        ProviderOutcomeUnknown("timeout"),
        ProviderUnavailable("503"),
        ValueError("bad response"),
    ],
)
async def test_uncertain_sent_never_posts_elsewhere_or_releases_hold(error: Exception) -> None:
    pool = Pool()
    pool.a.error = error
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert pool.operation.status is OperationStatus.OUTCOME_UNKNOWN
    assert pool.b.create_calls == 0 and pool.server.credential_account_id == "a"
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert pool.a.create_calls == 1
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_uncertain_recovery_uses_exact_digest_identity_on_attempted_account() -> None:
    pool = Pool()
    pool.a.error = ProviderOutcomeUnknown("timeout")
    await pool.process()
    pool.operation.updated_at = STALE
    assert await pool.reconciler.reconcile() == {ReconciliationOutcome.LEFT_UNCHANGED: 1}
    assert pool.a.recoveries == [
        (OP_KEY, {"legacy_label": False, "platform_server_id": str(pool.server.id)}),
    ]
    assert pool.b.recoveries == [] and pool.b.create_calls == 0
    pool.a.recovery = OrderRecoveryResult(OrderRecoveryVerdict.MATCHED, "101", 1)
    assert await pool.reconciler.reconcile() == {ReconciliationOutcome.RECOVERED: 1}
    assert pool.server.provider_server_id == "101"
    assert pool.operation.status is OperationStatus.COMPLETED
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_failed_refusal_write_stops_fallback() -> None:
    pool = Pool()
    pool.a.error = ProviderCapacityError(
        "full",
        error_code="resource_limit_exceeded",
        definitive_refusal=True,
    )
    pool.receipts.fail_refusal = True
    with pytest.raises(RuntimeError, match="acknowledgment"):
        await pool.process()
    assert pool.b.create_calls == 0 and last_attempt(pool.operation)["phase"] == "sent"
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_refused_crash_resumes_with_frozen_image_not_new_default() -> None:
    pool = Pool()
    pool.a.error = ProviderCapacityError(
        "full",
        error_code="resource_limit_exceeded",
        definitive_refusal=True,
    )
    pool.usage.counts["b"] = ProviderUnavailable("inventory unavailable")
    assert await pool.process() is ProvisioningOutcome.REQUEUED
    assert last_attempt(pool.operation)["phase"] == "capacity_refused"
    pool.b.images[1] = type(pool.b.images[1])("img-linux", "zz-debian", "linux", "x86")
    pool.b.images.insert(0, type(pool.b.images[1])("new-default", "aaa-linux", "linux", "x86"))
    pool.usage.counts["b"] = 2
    assert await pool.process() is ProvisioningOutcome.PROVISIONED
    assert pool.b.requests[0].image_id == "img-linux" and pool.a.create_calls == 1


async def test_all_full_releases_once_without_any_post() -> None:
    pool = Pool()
    pool.usage.counts.update(a=5, b=5)
    assert await pool.process() is ProvisioningOutcome.FAILED
    assert pool.a.create_calls == pool.b.create_calls == 0
    assert pool.server.state is ServerLifecycleState.ERROR
    pool.deps.holds.release_hold.assert_awaited_once()
    assert await pool.process() is ProvisioningOutcome.SKIPPED_STATE
    pool.deps.holds.release_hold.assert_awaited_once()


async def test_unreadable_inventory_requeues_without_full_failure_or_post() -> None:
    pool = Pool()
    pool.usage.counts.update(a=ProviderUnavailable("offline"), b=5)
    assert await pool.process() is ProvisioningOutcome.REQUEUED
    assert pool.a.create_calls == pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_fenced_worker_cannot_post_or_release_hold() -> None:
    pool = Pool()

    async def steal_stale_claim() -> None:
        await pool.receipts.resume_safe_claim(pool.operation.id, pool.operation.attempts)

    pool.receipts.before_sent = steal_stale_claim
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert pool.operation.status is OperationStatus.PENDING
    assert pool.a.create_calls == pool.b.create_calls == 0
    assert pool.server.credential_account_id == "a"
    pool.deps.holds.release_hold.assert_not_awaited()


@pytest.mark.parametrize("modern", [True, False])
@pytest.mark.parametrize("historical_pending", [True, False])
async def test_dedicated_create_intents_are_excluded_from_direct_calls_and_timeout_scans(
    modern: bool,
    historical_pending: bool,
) -> None:
    pool = Pool()
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    op.mark_in_flight()
    op.updated_at = STALE
    if historical_pending:
        op.requeue("historical timeout")
    if modern:
        pool.server.offer_fingerprint = {"fingerprint_version": 2}
    else:
        pool.server.billing_model = BILLING_MODEL_PREPAID_MONTHLY
    assert await pool.process() is ProvisioningOutcome.SKIPPED_STATE
    assert await pool.deps.worker.run_once() == {}
    assert await pool.reconciler._reconcile_in_flight(op) is ReconciliationOutcome.SKIPPED
    expected = {} if historical_pending else {ReconciliationOutcome.SKIPPED: 1}
    assert await pool.reconciler.reconcile() == expected
    status = OperationStatus.PENDING if historical_pending else OperationStatus.IN_FLIGHT
    assert op.status is status and op.attempts == 1
    assert pool.a.create_calls == pool.b.create_calls == 0


@pytest.mark.parametrize("worker", [True, False])
@pytest.mark.parametrize(
    "state", [ServerLifecycleState.REQUESTED, ServerLifecycleState.PROVISIONING]
)
@pytest.mark.parametrize(
    "verdict",
    [
        OrderRecoveryVerdict.NO_MATCH,
        OrderRecoveryVerdict.AMBIGUOUS,
        OrderRecoveryVerdict.SCAN_FAILED,
    ],
)
async def test_historical_ambiguous_operation_never_gains_pool_authorization(
    worker: bool,
    state: ServerLifecycleState,
    verdict: OrderRecoveryVerdict,
) -> None:
    pool = Pool()
    pool.server.state = state
    pool.a.recovery = OrderRecoveryResult(verdict)
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    op.mark_in_flight()
    op.requeue("historical timeout")
    op.updated_at = STALE
    if worker:
        assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    else:
        assert await pool.reconciler.reconcile() == {ReconciliationOutcome.LEFT_UNCHANGED: 1}
    assert op.status is OperationStatus.OUTCOME_UNKNOWN
    assert pool.a.recoveries == [
        (OP_KEY, {"legacy_label": True, "platform_server_id": str(pool.server.id)}),
    ]
    assert create_routing(op) is None and pool.a.create_calls == pool.b.create_calls == 0
    assert pool.server.credential_account_id == "a"
    assert pool.server.state is state and pool.server.provider_server_id is None
    assert pool.b.recoveries == []
    pool.routes.list_for_location.assert_not_awaited()
    pool.deps.holds.release_hold.assert_not_awaited()


@pytest.mark.parametrize("pin", [None, "removed"])
@pytest.mark.parametrize("historical_pending", [True, False])
async def test_historical_removed_or_default_ownership_fails_closed(
    pin: str | None,
    historical_pending: bool,
) -> None:
    pool = Pool()
    pool.server.credential_account_id = pin
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    op.mark_in_flight()
    if historical_pending:
        op.requeue("historical timeout")
        assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    op.updated_at = STALE
    assert await pool.reconciler.reconcile() == {ReconciliationOutcome.LEFT_UNCHANGED: 1}
    assert pool.a.recoveries == pool.b.recoveries == []
    assert pool.a.create_calls == pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()
    assert pool.server.credential_account_id == pin and create_routing(op) is None
    pool.routes.list_for_location.assert_not_awaited()


@pytest.mark.parametrize("code", [None, "resource_unavailable"])
async def test_non_documented_capacity_refusal_never_switches(code: str | None) -> None:
    pool = Pool()
    pool.a.error = ProviderCapacityError(
        "not a documented quota refusal",
        error_code=code,
        definitive_refusal=True,
    )
    assert await pool.process() is ProvisioningOutcome.FAILED
    assert pool.a.create_calls == 1 and pool.b.create_calls == 0
    assert last_attempt(pool.operation)["phase"] == "refused"
    pool.deps.holds.release_hold.assert_awaited_once()


async def test_alternative_without_frozen_image_is_never_posted() -> None:
    pool = Pool()
    pool.a.error = ProviderCapacityError(
        "full",
        error_code="resource_limit_exceeded",
        definitive_refusal=True,
    )
    pool.b.images = [type(pool.b.images[0])("different-linux", "debian", "linux", "x86")]
    assert await pool.process() is ProvisioningOutcome.FAILED
    assert pool.a.create_calls == 1 and pool.b.create_calls == 0
    assert pool.server.offer_fingerprint["legacy_create_request"]["image_id"] == "img-linux"
    pool.deps.holds.release_hold.assert_awaited_once()


async def test_no_sent_crash_safely_requeues_and_fences_generation() -> None:
    pool = Pool()
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    old = await pool.receipts.claim(op.id, pool.server.id)
    op.updated_at = STALE
    assert await pool.reconciler.reconcile() == {ReconciliationOutcome.REQUEUED: 1}
    assert op.status is OperationStatus.PENDING and op.attempts > old.attempts
    assert await pool.process() is ProvisioningOutcome.PROVISIONED
    assert pool.a.create_calls == 1


async def test_malformed_success_stops_failover_and_retains_reservation() -> None:
    pool = Pool()

    async def malformed(request: Any, key: Any) -> ProviderServer:
        pool.a.create_calls += 1
        return ProviderServer("", request.name, "initializing")

    pool.a.create_server = malformed
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert last_attempt(pool.operation)["phase"] == "outcome_unknown"
    assert pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_historical_read_only_no_match_does_not_authorize_new_pool_attempt() -> None:
    pool = Pool()
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    op.mark_in_flight()
    op.updated_at = STALE
    assert await pool.reconciler.reconcile() == {ReconciliationOutcome.LEFT_UNCHANGED: 1}
    assert pool.a.recoveries == [
        (OP_KEY, {"legacy_label": True, "platform_server_id": str(pool.server.id)}),
    ]
    assert create_routing(op) is None and op.status is OperationStatus.OUTCOME_UNKNOWN
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert pool.a.create_calls == pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_non_authoritative_capacity_exception_is_unknown_after_sent() -> None:
    pool = Pool()
    pool.a.error = ProviderCapacityError("local headroom check is not a POST refusal")
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert last_attempt(pool.operation)["phase"] == "outcome_unknown"
    assert pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_accepted_identity_survives_completion_failure_without_release_or_repost() -> None:
    pool = Pool()
    original = pool.receipts.save_outcome

    async def fail_completion(*args: Any, **kwargs: Any) -> Operation:
        if args[2] is OperationStatus.COMPLETED:
            raise RuntimeError("completion acknowledgment lost")
        return await original(*args, **kwargs)

    pool.receipts.save_outcome = fail_completion
    with pytest.raises(RuntimeError, match="completion"):
        await pool.process()
    assert last_attempt(pool.operation)["phase"] == "accepted"
    assert pool.server.provider_server_id == "101" and pool.server.credential_account_id == "a"
    pool.deps.holds.release_hold.assert_not_awaited()
    pool.receipts.save_outcome = original
    pool.operation.updated_at = STALE
    assert await pool.reconciler.reconcile() == {ReconciliationOutcome.RECOVERED: 1}
    assert pool.a.create_calls == 1 and pool.b.create_calls == 0
    assert pool.operation.status is OperationStatus.COMPLETED
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_stale_accepted_worker_cannot_finalize_or_release_funds() -> None:
    pool = Pool()
    original = pool.receipts.record_acceptance

    async def fence_after_acceptance(*args: Any, **kwargs: Any) -> Operation:
        accepted = await original(*args, **kwargs)
        await pool.receipts.resume_safe_claim(accepted.id, accepted.attempts)
        return accepted

    pool.receipts.record_acceptance = fence_after_acceptance
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert last_attempt(pool.operation)["phase"] == "accepted"
    assert pool.operation.status is OperationStatus.IN_FLIGHT
    assert pool.server.provider_server_id == "101" and pool.server.credential_account_id == "a"
    assert pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()


@pytest.mark.parametrize("pin", ["a", "removed"])
async def test_historical_completed_correlation_repairs_only_original_credential(pin: str) -> None:
    pool = Pool()
    pool.server.credential_account_id = pin
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    op.mark_in_flight()
    op.complete({"provider_server_id": "101", "idempotency_key": OP_KEY})
    outcome = await pool.process()
    if pin == "a":
        assert outcome is ProvisioningOutcome.ALREADY_PROVISIONED
        assert pool.server.provider_server_id == "101"
    else:
        assert outcome is ProvisioningOutcome.SKIPPED_IN_FLIGHT
        assert pool.server.provider_server_id is None
    assert create_routing(op) is None
    assert pool.a.create_calls == pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()


async def test_receipt_and_server_ownership_disagreement_never_mutates_resource() -> None:
    pool = Pool()
    assert await pool.process() is ProvisioningOutcome.PROVISIONED
    pool.server.credential_account_id = "b"
    assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    assert pool.server.provider_server_id == "101"
    assert pool.a.create_calls == 1 and pool.b.create_calls == 0
    pool.deps.holds.release_hold.assert_not_awaited()


@pytest.mark.parametrize("worker", [True, False])
async def test_terminal_refusal_crash_fails_before_releasing_without_routing(worker: bool) -> None:
    pool = Pool()
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    claimed = await pool.receipts.claim(op.id, pool.server.id)
    sent = await pool.receipts.start_attempt(
        op.id,
        claimed.attempts,
        "b",
        request_facts={
            "name": f"srv-{pool.server.id.hex[:8]}",
            "plan_id": "cx22",
            "location_id": "fsn1",
            "image_id": "img-linux",
        },
    )
    await pool.receipts.record_refusal(op.id, sent.attempts, "b", capacity=False)
    op.updated_at = STALE
    finalized = AsyncMock(wraps=pool.receipts.save_outcome)
    pool.receipts.save_outcome = finalized

    async def release_after_failure(hold_id: Any) -> None:
        assert op.status is OperationStatus.FAILED
        assert pool.server.state is ServerLifecycleState.ERROR
        assert op.attempts == sent.attempts + 1
        assert last_attempt(op)["phase"] == "refused"

    pool.deps.holds.release_hold.side_effect = release_after_failure
    if worker:
        assert await pool.process() is ProvisioningOutcome.FAILED
    else:
        assert await pool.reconciler.reconcile() == {ReconciliationOutcome.FAILED: 1}
    finalized.assert_awaited_once_with(
        op.id,
        sent.attempts + 1,
        OperationStatus.FAILED,
        error="provider definitively refused the create request",
    )
    assert pool.server.credential_account_id == "b" and pool.server.provider_server_id is None
    assert pool.a.create_calls == pool.b.create_calls == 0
    assert pool.a.recoveries == pool.b.recoveries == []
    pool.routes.list_for_location.assert_not_awaited()
    pool.deps.holds.release_hold.assert_awaited_once()
    with pytest.raises(CreateAttemptConflict, match="stale"):
        await pool.receipts.save_outcome(op.id, sent.attempts, OperationStatus.FAILED)
    assert await pool.process() is ProvisioningOutcome.SKIPPED_STATE
    assert await pool.reconciler.reconcile() == {}
    pool.deps.holds.release_hold.assert_awaited_once()


@pytest.mark.parametrize("worker", [True, False])
async def test_terminal_refusal_failed_finalization_keeps_hold_and_pin(worker: bool) -> None:
    pool = Pool()
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    claimed = await pool.receipts.claim(op.id, pool.server.id)
    await pool.receipts.start_attempt(
        op.id,
        claimed.attempts,
        "a",
        request_facts={
            "name": f"srv-{pool.server.id.hex[:8]}",
            "plan_id": "cx22",
            "location_id": "fsn1",
            "image_id": "img-linux",
        },
    )
    await pool.receipts.record_refusal(op.id, claimed.attempts, "a", capacity=False)
    op.updated_at = STALE
    pool.receipts.save_outcome = AsyncMock(side_effect=CreateAttemptConflict("lost claim"))
    if worker:
        assert await pool.process() is ProvisioningOutcome.SKIPPED_IN_FLIGHT
    else:
        assert await pool.reconciler.reconcile() == {ReconciliationOutcome.LEFT_UNCHANGED: 1}
    assert op.status is OperationStatus.IN_FLIGHT
    assert last_attempt(op)["phase"] == "refused"
    assert pool.server.state is ServerLifecycleState.REQUESTED
    assert pool.server.credential_account_id == "a"
    assert pool.a.create_calls == pool.b.create_calls == 0
    pool.routes.list_for_location.assert_not_awaited()
    pool.deps.holds.release_hold.assert_not_awaited()


@pytest.mark.parametrize("worker", [True, False])
async def test_historical_pending_match_recovers_original_account_without_receipt(
    worker: bool,
) -> None:
    pool = Pool()
    pool.server.credential_account_id = "b"
    pool.b.recovery = OrderRecoveryResult(OrderRecoveryVerdict.MATCHED, "202", 1)
    op = await pool.deps.ops.get_or_create(
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=pool.server.id,
        provider_key="hetzner",
    )
    op.mark_in_flight()
    op.requeue("historical ambiguous POST")
    op.updated_at = STALE
    if worker:
        assert await pool.process() is ProvisioningOutcome.PROVISIONED
    else:
        assert await pool.reconciler.reconcile() == {ReconciliationOutcome.RECOVERED: 1}
    assert pool.b.recoveries == [
        (OP_KEY, {"legacy_label": True, "platform_server_id": str(pool.server.id)}),
    ]
    assert pool.a.recoveries == []
    assert op.status is OperationStatus.COMPLETED and create_routing(op) is None
    assert pool.server.credential_account_id == "b"
    assert pool.server.provider_server_id == "202"
    assert pool.server.state is ServerLifecycleState.PROVISIONING
    assert pool.a.create_calls == pool.b.create_calls == 0
    assert await pool.process() is ProvisioningOutcome.ALREADY_PROVISIONED
    pool.routes.list_for_location.assert_not_awaited()
    pool.deps.holds.release_hold.assert_not_awaited()
