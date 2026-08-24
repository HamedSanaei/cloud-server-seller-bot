"""Provisioning worker (M07-002).

Executes server-create intents: for each ``REQUESTED`` server the worker
resolves the operation intent, claims it, calls the provider **once per
operation intent**, and records the correlation between the intent and the
provider-side result.

Exactly-once semantics (the acceptance property):
- One operation row per server create intent, keyed by the deterministic
  ``server-create:{server_id}`` (unique in the ledger).
- The operation key is **also** the ``IdempotencyKey`` sent to the provider,
  so the provider applies the mutation at most once per intent — a re-send
  after an unresolved failure returns the same resource, never a duplicate.
- Claims are conditional DB updates (PENDING -> IN_FLIGHT), so two workers
  cannot execute the same operation concurrently.
- The correlation (provider server id + status + the key used) is persisted
  on the operation before the server state advances.

Retryable provider failures re-queue the operation (same key); permanent
failures fail it, move the server to ERROR, and release the wallet hold so
no funds stay reserved for a server that will never be provisioned.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar, NoReturn, Protocol
from uuid import UUID, uuid4

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.audit.domain import ActorType, AuditRepository
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.billing.service import FinalChargeService, MissingSnapshotError
from cloud_platform.modules.compute.domain import (
    CloudServer,
    ServerLifecycleState,
    ServerRepository,
)
from cloud_platform.modules.navigation.domain import Callback, encode_callback
from cloud_platform.modules.notifications.domain import ProvisioningProgressService
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationRepository,
    OperationStatus,
    OperationType,
    ProviderServerState,
    StateAction,
    normalize_provider_status,
    plan_state_repair,
)
from cloud_platform.modules.wallet.domain import (
    HoldRepository,
    HoldStatus,
    WalletRepository,
)
from cloud_platform.modules.wallet.repository import HoldService
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.base import (
    Capability,
    CloudProvider,
    CreateServerRequest,
    ProviderImage,
    ProviderServer,
    power_probe_of,
    rebuild_support_of,
)
from cloud_platform.providers.errors import ProviderError, ProviderNotFound
from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.retry import ErrorClass, classify_provider_error
from cloud_platform.providers.waiter import (
    ActionWaiter,
    WaitOutcome,
    WaitProbe,
    WaitState,
)

logger = logging.getLogger(__name__)

RESOURCE_TYPE_SERVER = "server"

# Label the platform writes onto every provider server it creates. The orphan
# detector (M07-009) uses it to map a provider resource back to a platform row.
PLATFORM_SERVER_ID_LABEL = "platform_server_id"


class ImageSelector(Protocol):
    """Chooses the image for a provisioning request from the provider."""

    async def select_image(self, provider: CloudProvider) -> ProviderImage | None:
        """Return the chosen image, or None when the provider has no usable one."""
        ...


class FirstLinuxImageSelector:
    """Deterministic default: the first ``linux`` image by name.

    A placeholder policy until user-facing image selection exists; it is
    injectable so the real strategy can replace it without touching the worker.
    """

    async def select_image(self, provider: CloudProvider) -> ProviderImage | None:
        images = await provider.list_images()
        linux = sorted(
            (image for image in images if image.os_family.lower() == "linux"),
            key=lambda image: image.name,
        )
        return linux[0] if linux else None


def server_operation_key(server_id: UUID) -> str:
    """The deterministic operation key for a server create intent."""
    return f"server-create:{server_id}"


def build_create_request(server_id: UUID, spec: Any, image: ProviderImage) -> CreateServerRequest:
    """Build the provider create request (shared by worker and reconciler).

    The deterministic name and the platform label keep re-sends identical so
    the provider can deduplicate them by the idempotency key alone.
    """
    return CreateServerRequest(
        name=f"srv-{server_id.hex[:8]}",
        plan_id=spec.plan_id,
        image_id=image.id,
        location_id=spec.location_id,
        labels={PLATFORM_SERVER_ID_LABEL: str(server_id)},
    )


class ProvisioningOutcome(StrEnum):
    PROVISIONED = "provisioned"
    ALREADY_PROVISIONED = "already_provisioned"
    SKIPPED_IN_FLIGHT = "skipped_in_flight"
    SKIPPED_STATE = "skipped_state"
    REQUEUED = "requeued"
    FAILED = "failed"
    SKIPPED_RATE_LIMITED = "skipped_rate_limited"


class ProvisioningWorker:
    """Executes server-create intents against providers, exactly once each."""

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        image_selector: ImageSelector,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        audit_repo: AuditRepository,
        power_executor: PowerOperationExecutor | None = None,
        concurrency_limit: int = 3,
        waiter: ActionWaiter | None = None,
        progress: ProvisioningProgressService | None = None,
    ) -> None:
        if concurrency_limit < 1:
            raise ValueError("concurrency_limit must be at least 1")
        self._ops = operation_repo
        self._servers = server_repo
        self._registry = provider_registry
        self._images = image_selector
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._audit = AuditTrail(audit_repo)
        self._concurrency_limit = concurrency_limit
        self._waiter = waiter
        self._progress = progress
        self._power = power_executor or PowerOperationExecutor(
            operation_repo=operation_repo,
            server_repo=server_repo,
            provider_registry=provider_registry,
            audit_repo=audit_repo,
        )

    @staticmethod
    def _operation_key(server_id: UUID) -> str:
        return server_operation_key(server_id)

    async def process_server(self, server_id: UUID) -> ProvisioningOutcome:
        """Process one server's create intent. See module docstring for semantics.

        M11-002: runs inside a span; when the create operation carries the
        traceparent of the enqueuing request, the span joins that trace so
        one trace shows API -> job -> provider.
        """
        from cloud_platform.observability.tracing import operation_span

        async with operation_span(
            "provision server",
            traceparent=await self._create_traceparent(server_id),
            attributes={"cloud.resource.id": str(server_id)},
        ):
            return await self._process_server_in_span(server_id)

    async def _create_traceparent(self, server_id: UUID) -> str | None:
        """The persisted traceparent of an existing create op, else None.

        Best-effort: test doubles without ``get_by_key`` simply get no parent.
        """
        get_by_key = getattr(self._ops, "get_by_key", None)
        if get_by_key is None:
            return None
        existing = await get_by_key(self._operation_key(server_id))
        return existing.traceparent if existing is not None else None

    async def _process_server_in_span(self, server_id: UUID) -> ProvisioningOutcome:
        server = await self._servers.get(server_id)
        if server is None:
            return ProvisioningOutcome.SKIPPED_STATE
        if server.state is not ServerLifecycleState.REQUESTED and (
            server.state is not ServerLifecycleState.PROVISIONING
        ):
            # Not a create intent we may touch (running/deleting/deleted/...).
            return ProvisioningOutcome.SKIPPED_STATE

        operation = await self._ops.get_or_create(
            operation_key=self._operation_key(server_id),
            operation_type=OperationType.SERVER_CREATE,
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=server_id,
            provider_key=server.provider_key,
        )

        # Terminal operations are idempotent no-ops (or recorded failures).
        if operation.is_terminal:
            if operation.status is OperationStatus.COMPLETED:
                return await self._recover_from_correlation(server, operation)
            return ProvisioningOutcome.FAILED
        if operation.status is OperationStatus.IN_FLIGHT:
            # Another worker owns the attempt right now.
            return ProvisioningOutcome.SKIPPED_IN_FLIGHT

        claimed = await self._ops.claim(operation.id)
        if claimed is None:
            return ProvisioningOutcome.SKIPPED_IN_FLIGHT

        spec = await self._servers.get_provisioning_spec(server_id)
        if spec is None:
            return await self._fail_permanent(server, claimed, "catalog offer missing for server")

        try:
            provider: CloudProvider = self._registry.get(server.provider_key)
        except KeyError:
            return await self._fail_permanent(
                server, claimed, f"unknown provider {server.provider_key!r}"
            )

        image = await self._images.select_image(provider)
        if image is None:
            return await self._fail_permanent(
                server, claimed, f"no image available from provider {server.provider_key!r}"
            )

        request = build_create_request(server_id, spec, image)
        try:
            created: ProviderServer = await provider.create_server(
                request, IdempotencyKey(claimed.operation_key)
            )
        except ProviderError as exc:
            if classify_provider_error(exc) is ErrorClass.RETRYABLE:
                claimed.requeue(str(exc))
                await self._ops.save(claimed)
                await self._audit.record_mutation(
                    actor_type=ActorType.SYSTEM,
                    action="server.provisioning_requeued",
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=str(server_id),
                    reason=str(exc),
                    metadata={"operation_id": str(claimed.id), "attempts": claimed.attempts},
                )
                return ProvisioningOutcome.REQUEUED
            return await self._fail_permanent(server, claimed, str(exc))

        correlation: dict[str, object] = {
            "provider_server_id": created.id,
            "provider_status": created.status,
            "idempotency_key": claimed.operation_key,
        }
        claimed.complete(correlation)
        await self._ops.save(claimed)

        server.provider_server_id = created.id
        if server.state is ServerLifecycleState.REQUESTED:
            server.transition_to(ServerLifecycleState.PROVISIONING)
        await self._servers.save(server)

        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            action="server.provisioning_started",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server_id),
            reason=f"provider created server {created.id}",
            metadata={
                "operation_id": str(claimed.id),
                "provider_server_id": created.id,
                "provider_status": created.status,
                "idempotency_key": claimed.operation_key,
            },
        )
        logger.info("provisioned server %s -> provider %s", server_id, created.id)
        if self._progress is not None:
            await self._progress.started(server, f"provider server {created.id}")
        await self._watch_creation(server, provider, created)
        return ProvisioningOutcome.PROVISIONED

    async def _watch_creation(
        self, server: CloudServer, provider: CloudProvider, created: ProviderServer
    ) -> None:
        """Wait for the provider to finish creating (M07-004 waiter strategy).

        Optional and non-fatal: a completed wait fast-forwards the server to
        RUNNING; a timeout/failure leaves the server in PROVISIONING so the
        state reconciler (M07-005) keeps watching. Provider errors while
        polling are logged, not raised — the reconciler contains them.
        """
        waiter = self._waiter
        if waiter is None or not created.id:
            return

        async def probe() -> WaitProbe:
            remote = await provider.get_server(created.id)
            if remote is None:
                return WaitProbe(WaitState.FAILED, "server vanished after create")
            state = normalize_provider_status(remote.status)
            if state is ProviderServerState.RUNNING:
                return WaitProbe(WaitState.COMPLETED, remote.status)
            if state in (ProviderServerState.NOT_FOUND, ProviderServerState.DELETING):
                return WaitProbe(WaitState.FAILED, f"unexpected provider state: {state.value}")
            return WaitProbe(WaitState.PENDING, remote.status)

        try:
            result = await waiter.wait_for(probe)
        except ProviderError as exc:
            logger.warning(
                "creation watch for server %s interrupted by provider error; "
                "leaving reconciliation to the state reconciler: %s",
                server.id,
                exc,
            )
            return

        if result.outcome is WaitOutcome.COMPLETED:
            if server.state is ServerLifecycleState.PROVISIONING:
                server.transition_to(ServerLifecycleState.RUNNING)
                await self._servers.save(server)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                action="server.provisioning_wait_completed",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server.id),
                reason=f"provider finished creating in {result.polls} polls",
                metadata={
                    "provider_server_id": created.id,
                    "polls": str(result.polls),
                },
            )
            if self._progress is not None:
                await self._progress.succeeded(server, f"provider server {created.id}")
            return

        outcome_action = (
            "server.provisioning_wait_failed"
            if result.outcome is WaitOutcome.FAILED
            else "server.provisioning_wait_timeout"
        )
        logger.info(
            "creation watch for server %s ended %s after %d polls; state reconciler continues",
            server.id,
            result.outcome.value,
            result.polls,
        )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            action=outcome_action,
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            reason=result.detail or f"wait ended {result.outcome.value}",
            metadata={
                "provider_server_id": created.id,
                "polls": str(result.polls),
            },
        )

    async def run_once(self, limit: int = 10) -> dict[ProvisioningOutcome, int]:
        """Process up to ``limit`` REQUESTED servers; returns outcome counts.

        Per-account concurrency is capped at ``concurrency_limit``: a server
        is skipped (``SKIPPED_RATE_LIMITED``) when its provider account
        already has that many in-flight create operations, so parallel worker
        runs cannot push a provider past its quota/rate limit. The count is a
        fresh snapshot of IN_FLIGHT create ops taken once at the start of the
        run; the run itself processes servers sequentially (each op completes
        before the next starts), so at most one extra slot is in use.
        """
        async with metrics.job("provisioning_worker"):
            return await self._run_once(limit)

    async def _run_once(self, limit: int) -> dict[ProvisioningOutcome, int]:
        if limit <= 0:
            return {}
        counts: dict[ProvisioningOutcome, int] = {}
        candidates = (await self._servers.list_requested())[:limit]
        if not candidates:
            return counts
        in_flight_counts = await self._in_flight_counts_by_account()
        for server in candidates:
            account = (server.provider_key, server.provider_account_id)
            if in_flight_counts.get(account, 0) + 1 > self._concurrency_limit:
                counts[ProvisioningOutcome.SKIPPED_RATE_LIMITED] = (
                    counts.get(ProvisioningOutcome.SKIPPED_RATE_LIMITED, 0) + 1
                )
                continue
            outcome = await self.process_server(server.id)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    async def _in_flight_counts_by_account(self) -> dict[tuple[str, UUID], int]:
        """Map (provider_key, provider_account_id) -> in-flight create ops."""
        in_flight = await self._ops.list_in_flight(OperationType.SERVER_CREATE)
        counts: dict[tuple[str, UUID], int] = {}
        for op in in_flight:
            server = await self._servers.get(op.resource_id)
            if server is None:
                continue  # row gone (deletion); not occupying provider quota
            account = (server.provider_key, server.provider_account_id)
            counts[account] = counts.get(account, 0) + 1
        return counts

    async def process_pending_power(self, limit: int = 10) -> dict[str, int]:
        """Claim and execute PENDING power operations (crash recovery + retries).

        Returns counts keyed by ``executed`` / ``requeued`` / ``failed`` /
        ``contended`` (lost the claim race).
        """
        counts = {"executed": 0, "requeued": 0, "failed": 0, "contended": 0}
        if limit <= 0:
            return counts
        for op in (await self._ops.list_pending(POWER_OPERATION_TYPES))[:limit]:
            claimed = await self._ops.claim(op.id)
            if claimed is None:
                counts["contended"] += 1
                continue
            try:
                result = await self._power.execute(
                    claimed, actor_type=ActorType.SYSTEM, actor_id=None
                )
            except PowerOperationFailedError:
                counts["failed"] += 1
                continue
            counts["executed" if result is PowerExecutionResult.EXECUTED else "requeued"] += 1
        return counts

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _recover_from_correlation(
        self, server: CloudServer, operation: Operation
    ) -> ProvisioningOutcome:
        """A completed operation already has the correlation; apply it to the row."""
        correlation = operation.provider_response or {}
        provider_server_id: Any = correlation.get("provider_server_id")
        if isinstance(provider_server_id, str) and provider_server_id:
            if server.provider_server_id != provider_server_id:
                server.provider_server_id = provider_server_id
                if server.state is ServerLifecycleState.REQUESTED:
                    server.transition_to(ServerLifecycleState.PROVISIONING)
                await self._servers.save(server)
        return ProvisioningOutcome.ALREADY_PROVISIONED

    async def _fail_permanent(
        self, server: CloudServer, claimed: Operation, reason: str
    ) -> ProvisioningOutcome:
        claimed.fail(reason)
        await self._ops.save(claimed)
        await self._release_hold(server)
        # M11-006: the provisioning-failure alert feed.
        metrics.record_provisioning_failure("worker")
        if server.state in (
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
        ):
            try:
                server.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(server)
            except Exception:
                logger.exception(
                    "failed to mark server %s ERROR after permanent provisioning failure",
                    server.id,
                )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            action="server.provisioning_failed",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            reason=reason,
            metadata={"operation_id": str(claimed.id)},
        )
        if self._progress is not None:
            await self._progress.failed(server, reason)
        return ProvisioningOutcome.FAILED

    async def _release_hold(self, server: CloudServer) -> None:
        """Best-effort release of the create hold (keyed by the command key)."""
        if server.idempotency_key is None:
            return
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            return
        hold = await self._holds.get_by_idempotency(
            wallet.id, f"server-create:{server.idempotency_key}"
        )
        if hold is None or hold.id is None or hold.status is not HoldStatus.CREATED:
            return
        try:
            await self._holds.release_hold(hold.id)
        except Exception:
            logger.exception("failed to release hold %s for server %s", hold.id, server.id)


class ReconciliationOutcome(StrEnum):
    """Result of reconciling one timed-out create intent."""

    RECOVERED = "recovered"  # provider call (same key) succeeded; operation completed
    REQUEUED = "requeued"  # retryable error; operation back to PENDING for the worker
    FAILED = "failed"  # permanent error or vanished provider resource
    MARKED_FOR_REVIEW = "marked_for_review"  # uncertain state contained to MANUAL_REVIEW
    RECREATED_OPERATION = "recreated_operation"  # missing operation re-created (PENDING)
    LEFT_UNCHANGED = "left_unchanged"  # status check inconclusive; retry next round
    SKIPPED = "skipped"  # not timed out yet / not applicable


class CreateTimeoutReconciler:
    """Resolves timed-out create intents **without ever duplicating a server**.

    This is the safety net for the ambiguous window M07-002 leaves open: an
    operation that was IN_FLIGHT when the worker crashed, or a server stuck in
    PROVISIONING. Its two hard rules make a duplicate impossible:

    1. It **never issues a new idempotency key**. An ambiguous create is
       re-resolved by calling the provider again with the SAME operation key
       (the provider deduplicates and returns the same resource) or by
       verifying the already-recorded provider server id.
    2. It **never deletes a provider resource**. Any state it cannot resolve
       safely is contained to MANUAL_REVIEW for a human.

    A missing operation row for a REQUESTED server is recreated with the
    deterministic key (a PENDING operation has never called the provider), and
    a PROVISIONING server whose provider resource has vanished is failed with
    its hold released.
    """

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        image_selector: ImageSelector,
        wallet_repo: WalletRepository,
        hold_repo: HoldRepository,
        audit_repo: AuditRepository,
        in_flight_timeout: timedelta = timedelta(minutes=15),
        provisioning_timeout: timedelta = timedelta(hours=1),
        clock: Callable[[], datetime] | None = None,
        progress: ProvisioningProgressService | None = None,
    ) -> None:
        if in_flight_timeout <= timedelta(0) or provisioning_timeout <= timedelta(0):
            raise ValueError("timeouts must be positive")
        self._ops = operation_repo
        self._servers = server_repo
        self._registry = provider_registry
        self._images = image_selector
        self._wallets = wallet_repo
        self._holds = hold_repo
        self._audit = AuditTrail(audit_repo)
        self._in_flight_timeout = in_flight_timeout
        self._provisioning_timeout = provisioning_timeout
        self._now = clock or (lambda: datetime.now(UTC))
        self._progress = progress

    async def reconcile(self) -> dict[ReconciliationOutcome, int]:
        """Scan for timed-out create intents and resolve each safely.

        Returns a count per outcome. Every path obeys the two hard rules
        above. All candidate lists are snapshotted up front so one round
        acts on a single consistent view (a server recovered in step 1 is
        not re-scanned as PROVISIONING in step 3).
        """
        counts: dict[ReconciliationOutcome, int] = {}

        def bump(outcome: ReconciliationOutcome) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1
            # M11-006: the reconciliation drift alert feed (every outcome of
            # every round, per reconciler - drift = failed / marked_for_review).
            metrics.record_reconciliation("create", outcome.value)

        in_flight_ops = await self._ops.list_in_flight(OperationType.SERVER_CREATE)
        requested_servers = await self._servers.list_requested()
        provisioning_servers = await self._servers.list_provisioning()

        # 1) IN_FLIGHT create operations past the timeout (the ambiguous case).
        for op in in_flight_ops:
            age = self._age(op)
            if age is not None and age < self._in_flight_timeout:
                bump(ReconciliationOutcome.SKIPPED)
                continue
            bump(await self._reconcile_in_flight(op))

        # 2) REQUESTED servers whose operation row is missing (crash before the
        #    worker created it). Recreating it is safe: a PENDING operation has
        #    never called the provider, and the key is deterministic.
        for server in requested_servers:
            existing = await self._ops.get_by_key(server_operation_key(server.id))
            if existing is None:
                await self._ops.get_or_create(
                    operation_key=server_operation_key(server.id),
                    operation_type=OperationType.SERVER_CREATE,
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=server.id,
                    provider_key=server.provider_key,
                )
                bump(ReconciliationOutcome.RECREATED_OPERATION)

        # 3) PROVISIONING servers past the timeout (provider created it, but it
        #    never finished).
        for server in provisioning_servers:
            prov_op = await self._ops.get_by_key(server_operation_key(server.id))
            if prov_op is None or prov_op.status is not OperationStatus.COMPLETED:
                # Row/operation mismatch we cannot safely infer; contain it.
                await self._contain(
                    server,
                    f"provisioning timeout with no completed create operation "
                    f"(operation {prov_op.status.value if prov_op is not None else 'missing'})",
                )
                bump(ReconciliationOutcome.MARKED_FOR_REVIEW)
                continue
            age = self._age(prov_op)
            if age is not None and age < self._provisioning_timeout:
                bump(ReconciliationOutcome.SKIPPED)
                continue
            bump(await self._reconcile_provisioning(server, prov_op))

        return counts

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _age(self, operation: Operation) -> timedelta | None:
        """Time since the operation last moved; None means unknown (treat as stale)."""
        if operation.updated_at is None:
            return None
        return self._now() - operation.updated_at

    async def _reconcile_in_flight(self, op: Operation) -> ReconciliationOutcome:
        """Re-resolve an ambiguous IN_FLIGHT create with the SAME idempotency key."""
        server = await self._servers.get(op.resource_id)
        if server is None:
            # Server row gone; the intent can never be satisfied.
            op.fail("server row missing during create-timeout reconciliation")
            await self._ops.save(op)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                action="server.provisioning_failed",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(op.resource_id),
                reason="server row missing during create-timeout reconciliation",
                metadata={"operation_id": str(op.id)},
            )
            return ReconciliationOutcome.FAILED
        if server.state not in (
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
        ):
            # The intent is no longer live (deletion flow owns it); fail + refund.
            reason = f"server left the create window ({server.state.value})"
            op.fail(reason)
            await self._ops.save(op)
            await self._release_hold(server)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                action="server.provisioning_failed",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server.id),
                reason=reason,
                metadata={"operation_id": str(op.id)},
            )
            return ReconciliationOutcome.FAILED

        try:
            provider: CloudProvider = self._registry.get(op.provider_key)
        except KeyError:
            return await self._fail_intent(server, op, f"unknown provider {op.provider_key!r}")
        spec = await self._servers.get_provisioning_spec(op.resource_id)
        if spec is None:
            return await self._fail_intent(server, op, "catalog offer missing for server")
        image = await self._images.select_image(provider)
        if image is None:
            return await self._fail_intent(
                server, op, f"no image available from provider {op.provider_key!r}"
            )

        request = build_create_request(op.resource_id, spec, image)
        try:
            created: ProviderServer = await provider.create_server(
                request, IdempotencyKey(op.operation_key)
            )
        except ProviderError as exc:
            if classify_provider_error(exc) is ErrorClass.RETRYABLE:
                op.requeue(str(exc))
                await self._ops.save(op)
                await self._audit.record_mutation(
                    actor_type=ActorType.SYSTEM,
                    action="server.provisioning_requeued",
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=str(server.id),
                    reason=str(exc),
                    metadata={"operation_id": str(op.id)},
                )
                return ReconciliationOutcome.REQUEUED
            return await self._fail_intent(server, op, str(exc))

        # Success: the provider applied (or deduplicated) the SAME intent.
        op.complete(
            {
                "provider_server_id": created.id,
                "provider_status": created.status,
                "idempotency_key": op.operation_key,
            }
        )
        await self._ops.save(op)
        if server.provider_server_id != created.id:
            server.provider_server_id = created.id
            if server.state is ServerLifecycleState.REQUESTED:
                server.transition_to(ServerLifecycleState.PROVISIONING)
            await self._servers.save(server)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            action="server.provisioning_recovered",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            reason=f"reconciled after create timeout: provider server {created.id}",
            metadata={
                "operation_id": str(op.id),
                "provider_server_id": created.id,
                "idempotency_key": op.operation_key,
            },
        )
        return ReconciliationOutcome.RECOVERED

    async def _reconcile_provisioning(
        self, server: CloudServer, op: Operation
    ) -> ReconciliationOutcome:
        """A PROVISIONING server whose create already completed but never finished."""
        if not server.provider_server_id:
            await self._contain(server, "provisioning timeout with no recorded provider server id")
            return ReconciliationOutcome.MARKED_FOR_REVIEW
        try:
            provider: CloudProvider = self._registry.get(server.provider_key)
        except KeyError:
            await self._contain(
                server, f"provisioning timeout; unknown provider {server.provider_key!r}"
            )
            return ReconciliationOutcome.MARKED_FOR_REVIEW

        try:
            remote: ProviderServer | None = await provider.get_server(server.provider_server_id)
        except ProviderError:
            # Cannot conclude the resource is gone; leave it for the next round.
            return ReconciliationOutcome.LEFT_UNCHANGED

        if remote is None:
            # The provider resource vanished: the create failed at the provider.
            # M11-006: the provisioning-failure alert feed (reconciliation stage).
            metrics.record_provisioning_failure("reconciliation")
            try:
                server.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(server)
            except Exception:
                logger.exception(
                    "failed to mark server %s ERROR after lost provider resource",
                    server.id,
                )
            await self._release_hold(server)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                action="server.provisioning_lost",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server.id),
                reason=f"provider server {server.provider_server_id} not found on reconciliation",
                metadata={"operation_id": str(op.id)},
            )
            if self._progress is not None:
                await self._progress.failed(
                    server, "provider server not found during reconciliation"
                )
            return ReconciliationOutcome.FAILED

        # The resource exists but did not finish in time: uncertain, so a human
        # decides. We never delete it and never re-issue a create.
        await self._contain(
            server,
            f"provisioning timeout: provider server {server.provider_server_id} "
            f"still {remote.status!r}",
        )
        return ReconciliationOutcome.MARKED_FOR_REVIEW

    async def _fail_intent(
        self, server: CloudServer, op: Operation, reason: str
    ) -> ReconciliationOutcome:
        op.fail(reason)
        await self._ops.save(op)
        await self._release_hold(server)
        # M11-006: the provisioning-failure alert feed (intent stage).
        metrics.record_provisioning_failure("intent")
        if server.state in (
            ServerLifecycleState.REQUESTED,
            ServerLifecycleState.PROVISIONING,
        ):
            try:
                server.transition_to(ServerLifecycleState.ERROR)
                await self._servers.save(server)
            except Exception:
                logger.exception("failed to mark server %s ERROR during reconciliation", server.id)
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            action="server.provisioning_failed",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            reason=reason,
            metadata={"operation_id": str(op.id)},
        )
        if self._progress is not None:
            await self._progress.failed(server, reason)
        return ReconciliationOutcome.FAILED

    async def _contain(self, server: CloudServer, reason: str) -> None:
        """Move an uncertain server to MANUAL_REVIEW (remembers the prior state)."""
        try:
            server.contain()
            await self._servers.save(server)
        except Exception:
            logger.exception(
                "failed to move server %s to MANUAL_REVIEW on reconciliation",
                server.id,
            )
            return
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            action="server.provisioning_review",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            reason=reason,
        )

    async def _release_hold(self, server: CloudServer) -> None:
        if server.idempotency_key is None:
            return
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            return
        hold = await self._holds.get_by_idempotency(
            wallet.id, f"server-create:{server.idempotency_key}"
        )
        if hold is None or hold.id is None or hold.status is not HoldStatus.CREATED:
            return
        try:
            await self._holds.release_hold(hold.id)
        except Exception:
            logger.exception("failed to release hold for server %s", server.id)


class StateReconciliationOutcome(StrEnum):
    """Result of reconciling one server's state against the provider."""

    CONSISTENT = "consistent"  # local state matches the provider
    IN_PROGRESS = "in_progress"  # provider still creating; the waiter's job
    REPAIRED = "repaired"  # local row transitioned to match reality
    CONTAINED = "contained"  # unexpected drift -> MANUAL_REVIEW
    INCONCLUSIVE = "inconclusive"  # provider query failed; retry next round
    SKIPPED = "skipped"  # nothing to do (no provider id / unknown provider)


class ServerStateReconciler:
    """Maps drift between the local server row and provider reality (M07-005).

    For every server in a provider-backed state (PROVISIONING, RUNNING,
    STOPPED) the reconciler asks the provider for the live state and applies
    the pure plan from :func:`plan_state_repair`:

    - matching states -> left alone (CONSISTENT);
    - provider still creating -> left alone (IN_PROGRESS; the waiter watches it);
    - local/remote power mismatch (RUNNING<->STOPPED) or a finished
      PROVISIONING -> the row is transitioned to match reality (REPAIRED,
      audited ``server.state_reconciled``);
    - anything unexpected (provider deleting/unknown, resource vanished,
      rejected transition) -> contained to MANUAL_REVIEW (CONTAINED, audited
      ``server.state_review``).

    It **never deletes or destroys a provider resource** from reconciliation;
    deletion is only ever driven by an explicit user command.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
    ) -> None:
        self._servers = server_repo
        self._registry = provider_registry
        self._audit = AuditTrail(audit_repo)

    async def reconcile(self) -> dict[StateReconciliationOutcome, int]:
        """Reconcile all provider-backed servers; returns a count per outcome."""
        counts: dict[StateReconciliationOutcome, int] = {}

        def bump(outcome: StateReconciliationOutcome) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1

        servers = (
            await self._servers.list_provisioning()
            + await self._servers.list_running()
            + await self._servers.list_stopped()
        )
        for server in servers:
            bump(await self._reconcile_server(server))
        return counts

    async def _reconcile_server(self, server: CloudServer) -> StateReconciliationOutcome:
        if not server.provider_server_id:
            return StateReconciliationOutcome.SKIPPED
        try:
            provider: CloudProvider = self._registry.get(server.provider_key)
        except KeyError:
            return StateReconciliationOutcome.SKIPPED

        try:
            remote: ProviderServer | None = await provider.get_server(server.provider_server_id)
        except ProviderError:
            return StateReconciliationOutcome.INCONCLUSIVE

        remote_state = (
            ProviderServerState.NOT_FOUND
            if remote is None
            else normalize_provider_status(remote.status)
        )
        plan = plan_state_repair(server.state, remote_state)

        if plan.action is StateAction.NONE:
            return StateReconciliationOutcome.CONSISTENT
        if plan.action is StateAction.IN_PROGRESS:
            return StateReconciliationOutcome.IN_PROGRESS

        if plan.action is StateAction.REPAIR and plan.target is not None:
            old = server.state
            try:
                server.transition_to(plan.target)
                await self._servers.save(server)
            except Exception:
                logger.exception(
                    "planned state repair rejected for server %s; containing",
                    server.id,
                )
                return await self._contain(server, f"state repair rejected: {plan.reason}")
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                action="server.state_reconciled",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server.id),
                reason=f"provider state {remote_state.value}; {old.value} -> {plan.target.value}",
                metadata={
                    "provider_server_id": server.provider_server_id,
                    "from": old.value,
                    "to": plan.target.value,
                },
            )
            return StateReconciliationOutcome.REPAIRED

        # StateAction.CONTAIN
        return await self._contain(server, f"state reconciliation: {plan.reason}")

    async def _contain(self, server: CloudServer, reason: str) -> StateReconciliationOutcome:
        try:
            server.contain()
            await self._servers.save(server)
        except Exception:
            logger.exception(
                "failed to move server %s to MANUAL_REVIEW on state reconciliation",
                server.id,
            )
        await self._audit.record_mutation(
            actor_type=ActorType.SYSTEM,
            action="server.state_review",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            reason=reason,
            metadata={"provider_server_id": server.provider_server_id},
        )
        return StateReconciliationOutcome.CONTAINED


# ---------------------------------------------------------------------------
# Power commands (M07-006): on / off / reboot with ownership, capability and
# idempotency enforced in the application layer.
# ---------------------------------------------------------------------------


class PowerCommandError(Exception):
    """Base error for power command failures."""


class NotServerOwnerError(PowerCommandError):
    """The server does not belong to the acting user.

    The message deliberately does not reveal whether the server exists.
    """


class PowerActionNotAllowedError(PowerCommandError):
    """The server state, provider, or capabilities do not permit this action."""


class PowerOperationInProgressError(PowerCommandError):
    """Another attempt for this idempotency key is already in flight."""


class PowerOperationFailedError(PowerCommandError):
    """The power operation permanently failed (error recorded in the ledger)."""


class PowerAction(StrEnum):
    POWER_ON = "power_on"
    POWER_OFF = "power_off"
    REBOOT = "reboot"


_POWER_OP_TYPE: dict[PowerAction, OperationType] = {
    PowerAction.POWER_ON: OperationType.POWER_ON,
    PowerAction.POWER_OFF: OperationType.POWER_OFF,
    PowerAction.REBOOT: OperationType.REBOOT,
}

_POWER_AUDIT: dict[PowerAction, str] = {
    PowerAction.POWER_ON: "server.powered_on",
    PowerAction.POWER_OFF: "server.powered_off",
    PowerAction.REBOOT: "server.rebooted",
}

_OP_TYPE_TO_ACTION: dict[OperationType, PowerAction] = {v: k for k, v in _POWER_OP_TYPE.items()}

# Local states from which the action may be issued (the capability gate).
_POWER_PRECONDITION: dict[PowerAction, frozenset[ServerLifecycleState]] = {
    PowerAction.POWER_ON: frozenset({ServerLifecycleState.STOPPED}),
    PowerAction.POWER_OFF: frozenset({ServerLifecycleState.RUNNING}),
    PowerAction.REBOOT: frozenset({ServerLifecycleState.RUNNING}),
}

# Local state to apply once the provider confirms the action (None = unchanged).
_POWER_TARGET: dict[PowerAction, ServerLifecycleState | None] = {
    PowerAction.POWER_ON: ServerLifecycleState.RUNNING,
    PowerAction.POWER_OFF: ServerLifecycleState.STOPPED,
    PowerAction.REBOOT: None,
}

POWER_OPERATION_TYPES: tuple[OperationType, ...] = tuple(_POWER_OP_TYPE.values())


def power_operation_key(action: PowerAction, server_id: UUID, idempotency_key: str) -> str:
    """Deterministic ledger key for one power intent (unique per command key)."""
    return f"{action.value}:{server_id}:{idempotency_key}"


def available_power_actions(
    state: ServerLifecycleState, capabilities: set[Capability] | frozenset[Capability]
) -> list[PowerAction]:
    """The power actions valid for a local state and the provider's capabilities.

    The single source of the capability/state gate: a command is only issued
    for an action in this list, and the UI (M08-008) hides every action NOT
    in this list, so the user never sees a button that would be rejected.
    """
    return [
        action
        for action in PowerAction
        if state in _POWER_PRECONDITION[action] and Capability.POWER in capabilities
    ]


# ---------------------------------------------------------------------------
# Capability-driven server controls (M15-005)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerControl:
    """One control the server detail screen may offer, driven by a
    provider capability (M15-005).

    The table below is the SINGLE source that both the screen (which
    controls to render) and the command layer (which actions to accept)
    agree on: a control exists for a server iff its ``capability`` is in the
    provider's advertised set AND the server's local state is in
    ``state_gates``. Every other control is hidden, not shown disabled.

    Adding a new shared flow (e.g. snapshots when M13-004 ships) means
    adding one row here plus the nav transition - no per-provider UI code.
    """

    capability: Capability
    action: str  # nav action name (== PowerAction.value for power controls)
    label: str
    state_gates: frozenset[ServerLifecycleState]


#: The shared control table. Power controls reuse the M08-008 preconditions
#: verbatim; the table is provider-neutral - every provider renders exactly
#: the controls its capability set advertises (shared flows, M15-005).
SERVER_CONTROLS: tuple[ServerControl, ...] = (
    ServerControl(
        capability=Capability.POWER,
        action=PowerAction.POWER_ON.value,
        label="Power on",
        state_gates=_POWER_PRECONDITION[PowerAction.POWER_ON],
    ),
    ServerControl(
        capability=Capability.POWER,
        action=PowerAction.POWER_OFF.value,
        label="Power off",
        state_gates=_POWER_PRECONDITION[PowerAction.POWER_OFF],
    ),
    ServerControl(
        capability=Capability.POWER,
        action=PowerAction.REBOOT.value,
        label="Reboot",
        state_gates=_POWER_PRECONDITION[PowerAction.REBOOT],
    ),
)


def available_server_controls(
    state: ServerLifecycleState, capabilities: set[Capability] | frozenset[Capability]
) -> list[ServerControl]:
    """The controls available for one server: capability-gated AND
    state-gated, in table order. Unsupported controls are hidden, never
    shown disabled."""
    return [
        control
        for control in SERVER_CONTROLS
        if control.capability in capabilities and state in control.state_gates
    ]


@dataclass(frozen=True, slots=True)
class PowerCommandResult:
    server: CloudServer
    replayed: bool  # True when a prior attempt with the same key already completed
    requeued: bool = False  # True when the attempt hit a retryable provider error


class PowerExecutionResult(StrEnum):
    EXECUTED = "executed"
    REQUEUED = "requeued"


class PowerOperationExecutor:
    """Executes a *claimed* power operation against the provider.

    Shared by the interactive command path (actor = the issuing user) and the
    worker path (actor = system, retrying re-queued operations). The
    operation key is the ``IdempotencyKey`` sent to the provider, so a re-send
    after an unresolved failure can never trigger a second physical power
    action.
    """

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
    ) -> None:
        self._ops = operation_repo
        self._servers = server_repo
        self._registry = provider_registry
        self._audit = AuditTrail(audit_repo)

    async def execute(
        self,
        operation: Operation,
        *,
        actor_type: ActorType,
        actor_id: UUID | None = None,
    ) -> PowerExecutionResult:
        """Run one claimed (IN_FLIGHT) power operation; raises on permanent failure.

        M11-002: the whole execution runs inside a span that joins the trace
        of the request that enqueued the operation (via the persisted
        traceparent), so one trace shows API -> job -> provider.
        """
        from cloud_platform.observability.tracing import operation_span

        async with operation_span(
            "power operation",
            traceparent=operation.traceparent,
            attributes={
                "cloud.operation.id": str(operation.id),
                "cloud.operation.type": operation.operation_type.value,
                "cloud.operation.attempts": operation.attempts,
                "cloud.provider": operation.provider_key,
            },
        ):
            return await self._execute_in_span(operation, actor_type=actor_type, actor_id=actor_id)

    async def _execute_in_span(
        self,
        operation: Operation,
        *,
        actor_type: ActorType,
        actor_id: UUID | None = None,
    ) -> PowerExecutionResult:
        """The original execution body (runs inside the operation span)."""
        action = _OP_TYPE_TO_ACTION.get(operation.operation_type)
        if action is None:
            raise PowerCommandError(f"operation {operation.id} is not a power operation")
        server = await self._servers.get(operation.resource_id)
        if server is None:
            await self._fail(operation, actor_type, actor_id, "server row missing")
        if not server.provider_server_id:
            await self._fail(operation, actor_type, actor_id, "server has no provider resource id")
        if server.state not in _POWER_PRECONDITION[action]:
            # The state moved since the intent was created (drift, a newer
            # command, containment). Never call the provider against a
            # mismatched state.
            await self._fail(
                operation,
                actor_type,
                actor_id,
                f"server left the expected state ({server.state.value}) before "
                f"{action.value} was executed",
            )
        try:
            provider: CloudProvider = self._registry.get(server.provider_key)
        except KeyError:
            await self._fail(
                operation,
                actor_type,
                actor_id,
                f"unknown provider {server.provider_key!r}",
            )
        if Capability.POWER not in provider.capabilities:
            await self._fail(operation, actor_type, actor_id, "provider lacks POWER capability")

        # Ambiguous-mutation guard (M15-004): on a RE-SEND (an earlier attempt
        # exists) against a provider that can prove its power effects (no
        # native idempotency header, e.g. ArvanCloud), probe first so a
        # timed-out mutation is never blindly re-applied. Providers without a
        # probe (Hetzner) skip straight to the plain re-send below.
        if operation.attempts >= 2 and power_probe_of(provider) is not None:
            probe = await self._probe_power_effect(provider, server, action)
            if probe is True:
                return await self._complete_probe_applied(
                    operation, server, action, actor_type, actor_id
                )
            if probe is None and action is PowerAction.REBOOT:
                # Reboot is a transient action: steady state cannot prove it,
                # and re-sending would reboot a second time. Wait for the
                # next round; the state reconciler drives the row.
                operation.requeue(
                    f"{action.value} effect inconclusive after a timed-out attempt; "
                    "not re-sending to avoid a second physical action"
                )
                await self._ops.save(operation)
                await self._audit.record_mutation(
                    actor_type=actor_type,
                    action="server.power_probe_inconclusive",
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=str(server.id),
                    actor_id=actor_id,
                    reason="reboot effect inconclusive; re-queued instead of re-sent",
                    metadata={
                        "operation_id": str(operation.id),
                        "provider_server_id": server.provider_server_id,
                    },
                )
                return PowerExecutionResult.REQUEUED

        if not await self._invoke_power(operation, provider, server, action, actor_type, actor_id):
            return PowerExecutionResult.REQUEUED
        return await self._finish_power(operation, server, action, actor_type, actor_id)

    async def _probe_power_effect(
        self, provider: CloudProvider, server: CloudServer, action: PowerAction
    ) -> bool | None:
        """Call the optional probe; None when the provider has no probe or the
        probe itself fails (conservative default, never a re-send for reboot)."""
        probe_method = power_probe_of(provider)
        if probe_method is None:
            return None
        provider_server_id = server.provider_server_id
        if provider_server_id is None:
            return None
        try:
            result = await probe_method(provider_server_id, action.value)
            return result if result is None or isinstance(result, bool) else None
        except ProviderError:
            return None

    async def _invoke_power(
        self,
        operation: Operation,
        provider: CloudProvider,
        server: CloudServer,
        action: PowerAction,
        actor_type: ActorType,
        actor_id: UUID | None,
    ) -> bool:
        """Send the power mutation. Returns False when re-queued (retryable
        error); raises on permanent failure."""
        method = getattr(provider, action.value)
        try:
            await method(server.provider_server_id, IdempotencyKey(operation.operation_key))
        except ProviderError as exc:
            if classify_provider_error(exc) is ErrorClass.RETRYABLE:
                operation.requeue(str(exc))
                await self._ops.save(operation)
                await self._audit.record_mutation(
                    actor_type=actor_type,
                    action="server.power_requeued",
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=str(server.id),
                    actor_id=actor_id,
                    reason=str(exc),
                    metadata={"operation_id": str(operation.id)},
                )
                return False
            await self._fail(operation, actor_type, actor_id, str(exc))
        return True

    async def _finish_power(
        self,
        operation: Operation,
        server: CloudServer,
        action: PowerAction,
        actor_type: ActorType,
        actor_id: UUID | None,
    ) -> PowerExecutionResult:
        """Record completion, apply the target state, and audit the action."""
        operation.complete({"action": action.value, "idempotency_key": operation.operation_key})
        await self._ops.save(operation)
        target = _POWER_TARGET[action]
        if target is not None and server.state is not target:
            if server.state in _POWER_PRECONDITION[action]:
                try:
                    server.transition_to(target)
                    await self._servers.save(server)
                except Exception:
                    logger.exception(
                        "state transition after %s rejected for server %s; "
                        "the state reconciler will correct it",
                        action.value,
                        server.id,
                    )
        await self._audit.record_mutation(
            actor_type=actor_type,
            action=_POWER_AUDIT[action],
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=actor_id,
            reason=f"{action.value} completed",
            metadata={
                "operation_id": str(operation.id),
                "provider_server_id": server.provider_server_id,
            },
        )
        return PowerExecutionResult.EXECUTED

    async def _complete_probe_applied(
        self,
        operation: Operation,
        server: CloudServer,
        action: PowerAction,
        actor_type: ActorType,
        actor_id: UUID | None,
    ) -> PowerExecutionResult:
        """Probe proved the effect already holds: complete WITHOUT calling the
        provider (the ambiguous re-send never happens), then apply the same
        state transition and audit as a normal success."""
        operation.complete(
            {
                "action": action.value,
                "idempotency_key": operation.operation_key,
                "probe": "already-applied",
            }
        )
        await self._ops.save(operation)
        target = _POWER_TARGET[action]
        if target is not None and server.state is not target:
            if server.state in _POWER_PRECONDITION[action]:
                try:
                    server.transition_to(target)
                    await self._servers.save(server)
                except Exception:
                    logger.exception(
                        "state transition after probe-applied %s rejected for server %s",
                        action.value,
                        server.id,
                    )
        await self._audit.record_mutation(
            actor_type=actor_type,
            action=_POWER_AUDIT[action],
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=actor_id,
            reason=f"{action.value} probe: effect already applied (no provider call)",
            metadata={
                "operation_id": str(operation.id),
                "provider_server_id": server.provider_server_id,
                "probe": "already-applied",
            },
        )
        return PowerExecutionResult.EXECUTED

    async def _fail(
        self,
        operation: Operation,
        actor_type: ActorType,
        actor_id: UUID | None,
        reason: str,
    ) -> NoReturn:
        operation.fail(reason)
        await self._ops.save(operation)
        await self._audit.record_mutation(
            actor_type=actor_type,
            action="server.power_failed",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(operation.resource_id),
            actor_id=actor_id,
            reason=reason,
            metadata={"operation_id": str(operation.id)},
        )
        raise PowerOperationFailedError(reason)


class PowerCommandService:
    """User-facing power commands: power_on / power_off / reboot.

    Authorization is enforced here (the application layer), not the UI:
    - **Ownership** — the server must belong to the acting user; a server
      belonging to someone else (or not found) is indistinguishable.
    - **Capability** — the local state must allow the action, the provider
      must be known, and it must advertise the POWER capability.
    - **Idempotency** — one ledger operation per (action, server, command
      key); the operation key is the IdempotencyKey sent to the provider, so
      a retry of the same command can never double-act. A completed command
      is replayed without a provider call; an in-flight one is rejected; a
      failed one surfaces its recorded error.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        operation_repo: OperationRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
        executor: PowerOperationExecutor | None = None,
    ) -> None:
        self._servers = server_repo
        self._ops = operation_repo
        self._executor = executor or PowerOperationExecutor(
            operation_repo=operation_repo,
            server_repo=server_repo,
            provider_registry=provider_registry,
            audit_repo=audit_repo,
        )

    async def power_on(
        self, user_id: UUID, server_id: UUID, idempotency_key: str
    ) -> PowerCommandResult:
        return await self._execute(PowerAction.POWER_ON, user_id, server_id, idempotency_key)

    async def power_off(
        self, user_id: UUID, server_id: UUID, idempotency_key: str
    ) -> PowerCommandResult:
        return await self._execute(PowerAction.POWER_OFF, user_id, server_id, idempotency_key)

    async def reboot(
        self, user_id: UUID, server_id: UUID, idempotency_key: str
    ) -> PowerCommandResult:
        return await self._execute(PowerAction.REBOOT, user_id, server_id, idempotency_key)

    async def _execute(
        self,
        action: PowerAction,
        user_id: UUID,
        server_id: UUID,
        idempotency_key: str,
    ) -> PowerCommandResult:
        if not idempotency_key or not idempotency_key.strip():
            raise PowerCommandError("idempotency_key is required")
        key = power_operation_key(action, server_id, idempotency_key.strip())
        if len(key) > 128:
            raise PowerCommandError("idempotency_key too long")

        server = await self._servers.get(server_id)
        if server is None or server.user_id != user_id:
            raise NotServerOwnerError("server not found")

        # Replays of an existing intent are resolved from the ledger BEFORE any
        # state gate: a completed command must stay idempotent even after the
        # state it changed has moved on.
        op = await self._ops.get_by_key(key)
        state_valid = server.state in _POWER_PRECONDITION[action]
        if op is not None:
            if op.status is OperationStatus.COMPLETED:
                return PowerCommandResult(
                    server=await self._servers.get(server_id) or server, replayed=True
                )
            if op.status is OperationStatus.FAILED:
                raise PowerOperationFailedError(op.error or "power operation failed")
            if op.status is OperationStatus.IN_FLIGHT:
                raise PowerOperationInProgressError("power operation is in progress")
            # PENDING: only a brand-new (never claimed) intent may be gated on
            # the current state; a re-queued retry is left to the executor's
            # own state guard.
            if not state_valid and op.attempts == 0:
                raise PowerActionNotAllowedError(
                    f"{action.value} not allowed in state {server.state.value}"
                )
        else:
            if not state_valid:
                raise PowerActionNotAllowedError(
                    f"{action.value} not allowed in state {server.state.value}"
                )
            op = await self._ops.get_or_create(
                operation_key=key,
                operation_type=_POWER_OP_TYPE[action],
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=server_id,
                provider_key=server.provider_key,
            )

        claimed = await self._ops.claim(op.id)
        if claimed is None:
            raise PowerOperationInProgressError("power operation is in progress")

        result = await self._executor.execute(claimed, actor_type=ActorType.USER, actor_id=user_id)
        return PowerCommandResult(
            server=await self._servers.get(server_id) or server,
            replayed=False,
            requeued=result is PowerExecutionResult.REQUEUED,
        )


# ---------------------------------------------------------------------------
# Power controls UI (M08-008)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerActionOption:
    """One power button the user may press for their server."""

    action: PowerAction
    label: str
    callback: str


@dataclass(frozen=True, slots=True)
class ServerPowerView:
    """The servers.detail screen: the server's status + its power controls.

    Acceptance: **capabilities/states hide invalid actions.** ``actions``
    contains exactly the power actions that would be accepted by the power
    command for this server right now (local state gate AND the provider's
    POWER capability); every other action is hidden, not shown disabled, so
    the user never sees a button that would be rejected. The buttons carry
    signed callbacks (M08-001 scheme) that the bot dispatches into
    :class:`PowerCommandService` with a fresh idempotency key per press.
    """

    server_id: UUID
    provider_key: str
    state: ServerLifecycleState
    provider_server_id: str | None
    actions: tuple[PowerActionOption, ...]
    back_callback: str  # servers.list
    cancel_callback: str  # main.menu

    def render(self) -> str:
        """ASCII rendering for logs and the review UI."""
        lines = [
            f"Server {self.server_id} [{self.state.value}] @ {self.provider_key}"
            + (f" (provider {self.provider_server_id})" if self.provider_server_id else ""),
        ]
        if self.actions:
            lines.append("  power: " + ", ".join(opt.label for opt in self.actions))
        else:
            lines.append("  power: (no actions available)")
        return "\n".join(lines)


class PowerControlsService:
    """Builds the power controls of the server detail screen (M08-008).

    Acceptance: **capabilities/states hide invalid actions.** The set of
    actions is computed with the SAME gate the power command enforces
    (:func:`available_power_actions`: local state precondition + the
    provider's POWER capability), so the screen and the command can never
    disagree. An unknown provider (not in the registry) exposes no power
    actions at all. Ownership is enforced here as in MyServersService: a
    missing or foreign server is indistinguishable (``None``).

    No enforcement happens here: pressing a button calls
    PowerCommandService, which re-checks ownership, state and capability at
    execution time and is idempotent per command key.

    Capability-driven (M15-005): the control set comes from the shared
    ``SERVER_CONTROLS`` table - the SAME table the command gate uses - so
    every provider renders exactly the controls its advertised capabilities
    support, and shared flows are reused verbatim across providers.
    """

    _LABELS: ClassVar[dict[PowerAction, str]] = {
        PowerAction.POWER_ON: "Power on",
        PowerAction.POWER_OFF: "Power off",
        PowerAction.REBOOT: "Reboot",
    }

    def __init__(
        self,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        signing_key: str,
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._servers = server_repo
        self._registry = provider_registry
        self._signing_key = signing_key

    async def detail(self, user_id: UUID, server_id: UUID) -> ServerPowerView | None:
        """The detail screen of one server the user owns, or None."""
        server = await self._servers.get(server_id)
        if server is None or server.user_id != user_id:
            return None

        try:
            provider: CloudProvider = self._registry.get(server.provider_key)
        except KeyError:
            capabilities: set[Capability] = set()
        else:
            capabilities = set(provider.capabilities)

        controls = available_server_controls(server.state, capabilities)
        actions = tuple(
            PowerActionOption(
                action=PowerAction(control.action),
                label=control.label,
                callback=encode_callback(
                    Callback(
                        flow="servers",
                        screen=control.action,
                        args=(str(server.id),),
                    ),
                    self._signing_key,
                ),
            )
            for control in controls
        )
        return ServerPowerView(
            server_id=server.id,
            provider_key=server.provider_key,
            state=server.state,
            provider_server_id=server.provider_server_id,
            actions=actions,
            back_callback=encode_callback(
                Callback(flow="servers", screen="list"), self._signing_key
            ),
            cancel_callback=encode_callback(
                Callback(flow="main", screen="menu"), self._signing_key
            ),
        )


# ---------------------------------------------------------------------------
# Orphan provider-resource detector (M07-009)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrphanResource:
    """A provider server the platform created but that has no platform row.

    ``claimed_server_id`` is the ``platform_server_id`` label value: a valid
    UUID the row for which is missing, or a non-empty string that could not be
    parsed (still reported, so nothing is silently dropped).
    """

    provider_key: str
    provider_server_id: str
    name: str
    status: str
    claimed_server_id: str


def _claimed_server_id(server: ProviderServer) -> str | None:
    """Read the platform label off a provider server, if present and usable."""
    labels = server.metadata.get(PLATFORM_SERVER_ID_LABEL)
    if labels is None:
        # Hetzner nests labels under metadata["labels"]; other adapters may
        # place them at the top level, so check both.
        nested = server.metadata.get("labels")
        if isinstance(nested, dict):
            labels = nested.get(PLATFORM_SERVER_ID_LABEL)
    if not isinstance(labels, str):
        return None
    value = labels.strip()
    return value or None


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


class OrphanDetector:
    """Finds provider-side servers absent from the platform database.

    Scans every registered provider's servers and flags those carrying the
    ``platform_server_id`` label whose claimed platform row no longer exists.
    Such orphans are billed by the provider but cannot be billed to (or
    managed by) any user — a pure cost leak.

    The detector **never deletes** a provider resource. It reports and audits
    each orphan (``provider.orphan_detected``) so a human can reconcile the
    finances and delete it through the explicit, authorized path. A claimed
    id that still has a row in *any* state — including ``DELETED`` — is not an
    orphan: the deletion flow owns that row.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
    ) -> None:
        self._servers = server_repo
        self._registry = provider_registry
        self._audit = AuditTrail(audit_repo)

    async def detect(self) -> list[OrphanResource]:
        """Scan all providers; return (and audit) the orphans found."""
        orphans: list[OrphanResource] = []
        for key in self._registry.keys():
            provider = self._registry.get(key)
            try:
                remotes = await provider.list_servers()
            except ProviderError:
                logger.exception("orphan scan failed for provider %s", key)
                continue
            for remote in remotes:
                claimed = _claimed_server_id(remote)
                if claimed is None:
                    continue  # not created by this platform
                server_id = _parse_uuid(claimed)
                if server_id is not None:
                    existing = await self._servers.get(server_id)
                    if existing is not None:
                        continue  # row exists in any state; not an orphan
                orphans.append(
                    OrphanResource(
                        provider_key=key,
                        provider_server_id=remote.id,
                        name=remote.name,
                        status=remote.status,
                        claimed_server_id=claimed,
                    )
                )
        for orphan in orphans:
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                action="provider.orphan_detected",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=orphan.provider_server_id,
                reason=(
                    f"provider server {orphan.provider_server_id} "
                    f"({orphan.name!r}) has no platform row for "
                    f"{orphan.claimed_server_id!r}"
                ),
                metadata={
                    "provider_key": orphan.provider_key,
                    "claimed_server_id": orphan.claimed_server_id,
                    "status": orphan.status,
                },
            )
        return orphans


# ---------------------------------------------------------------------------
# Missing-provider-resource detector (M07-010)
# ---------------------------------------------------------------------------


# States in which the platform expects the provider resource to exist. Deletion
# states are excluded (absence there is expected); REQUESTED is excluded (not
# created yet); ERROR/MANUAL_REVIEW are already out of the healthy path.
_MISSING_SCAN_STATES: tuple[ServerLifecycleState, ...] = (
    ServerLifecycleState.PROVISIONING,
    ServerLifecycleState.RUNNING,
    ServerLifecycleState.STOPPED,
)


@dataclass(frozen=True, slots=True)
class MissingResource:
    """A platform server in an active state whose provider resource is absent."""

    provider_key: str
    provider_server_id: str
    server_id: UUID
    state: ServerLifecycleState


class MissingResourceDetector:
    """Finds DB-active servers whose provider resource no longer exists.

    The mirror of the orphan detector: instead of scanning the provider for
    resources without a platform row, it scans the platform for rows in an
    active state (PROVISIONING/RUNNING/STOPPED) whose
    ``provider_server_id`` is not found by the provider.

    It **reports and audits** each finding (``provider.missing_resource_detected``)
    but does **not** change server state and never deletes anything: turning a
    drifted row into a safe state / MANUAL_REVIEW is the state reconciler's job
    (M07-005); this detector gives operators the systematic, provider-wide view.
    A provider query error is inconclusive (the resource may be fine) and is
    counted, not reported.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
    ) -> None:
        self._servers = server_repo
        self._registry = provider_registry
        self._audit = AuditTrail(audit_repo)

    async def detect(self) -> list[MissingResource]:
        """Scan all active-state servers; return (and audit) the missing ones."""
        candidates: list[CloudServer] = []
        for state in _MISSING_SCAN_STATES:
            candidates.extend(await self._list_for_state(state))

        missing: list[MissingResource] = []
        for server in candidates:
            if not server.provider_server_id:
                continue  # nothing to check (provisioning window is M07-003's)
            try:
                provider = self._registry.get(server.provider_key)
            except KeyError:
                continue  # unknown provider; cannot check
            try:
                remote = await provider.get_server(server.provider_server_id)
            except ProviderError:
                logger.exception("missing-resource check inconclusive for server %s", server.id)
                continue
            if remote is None:
                missing.append(
                    MissingResource(
                        provider_key=server.provider_key,
                        provider_server_id=server.provider_server_id,
                        server_id=server.id,
                        state=server.state,
                    )
                )
        for m in missing:
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                action="provider.missing_resource_detected",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(m.server_id),
                reason=(
                    f"server {m.server_id} is {m.state.value} but provider "
                    f"{m.provider_key} has no resource {m.provider_server_id}"
                ),
                metadata={
                    "provider_key": m.provider_key,
                    "provider_server_id": m.provider_server_id,
                    "state": m.state.value,
                },
            )
        return missing

    async def _list_for_state(self, state: ServerLifecycleState) -> list[CloudServer]:
        if state is ServerLifecycleState.PROVISIONING:
            return await self._servers.list_provisioning()
        if state is ServerLifecycleState.RUNNING:
            return await self._servers.list_running()
        if state is ServerLifecycleState.STOPPED:
            return await self._servers.list_stopped()
        return []


# ---------------------------------------------------------------------------
# Delete-server saga (M07-007)
# ---------------------------------------------------------------------------
#
# Acceptance: delete request -> provider absent -> billing final -> deleted.
#
# The saga is one durable operation (key ``server-delete:{server_id}:{ik}``,
# the same key sent to the provider as its IdempotencyKey) executed as:
#
#   1. request: the command service (ownership + state gates) creates the
#      operation and moves the server to DELETE_REQUESTED;
#   2. provider absence: the executor transitions DELETE_REQUESTED -> DELETING,
#      calls provider.delete_server once per operation, treats 404
#      (ProviderNotFound) as success, and waits (bounded) until the provider
#      no longer returns the server; a timeout re-queues the SAME operation
#      so a later attempt re-verifies instead of double-acting;
#   3. billing final: once absence is confirmed the server gets its deletion
#      timestamp and transitions DELETING -> DELETED, and the final usage
#      segment is settled by FinalChargeService (M06-006; idempotent under
#      its own ledger keys, so a crash-replay settles nothing twice). A
#      server that never had a provider resource (failed before
#      provisioning) has no usage: any still-reserved creation hold is
#      released back to the wallet and no charge is made;
#   4. deleted: the operation completes with the correlation (deletion time,
#      charge amount) and the server row is DELETED (freeing quota).
#
# Every step is individually replay-safe, so a crash anywhere in the middle
# leaves a consistent, retryable state.


class DeleteCommandError(Exception):
    """Base error for the delete command path."""


class DeleteNotOwnerError(DeleteCommandError):
    """The acting user does not own the server (indistinguishable from absent)."""


class DeleteActionNotAllowedError(DeleteCommandError):
    """The server's state does not allow a delete request."""


class DeleteOperationInProgressError(DeleteCommandError):
    """A delete operation for this server is already running."""


class DeleteOperationFailedError(DeleteCommandError):
    """The delete operation permanently failed (error recorded in the ledger)."""


#: Local states from which a delete may be requested.
DELETE_PRECONDITION_STATES: frozenset[ServerLifecycleState] = frozenset(
    {
        ServerLifecycleState.RUNNING,
        ServerLifecycleState.STOPPED,
        ServerLifecycleState.ERROR,
        ServerLifecycleState.MANUAL_REVIEW,
    }
)


# ---------------------------------------------------------------------------
# Delete confirmation UI (M08-009)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeleteConfirmationView:
    """The delete.confirm screen: the double-confirmation of a server deletion.

    Acceptance: **double confirmation and idempotent callback.**

    - Stage 1 (``stage=1``) shows what will happen (irreversible, final
      charge at deletion). Pressing its ``confirm_callback`` advances to
      stage 2, which carries the SAME idempotency key and a stronger
      warning.
    - Only the stage-2 ``execute_callback`` runs the delete. It embeds the
      idempotency key the screen was built with, so a double-tap or a
      re-sent callback replays the SAME DeleteCommandService command
      (COMPLETED -> replayed, IN_FLIGHT -> rejected as in-progress) instead
      of issuing a second deletion.
    - The delete button is only shown when the delete command itself would
      accept the state (``DELETE_PRECONDITION_STATES``); anything else
      returns ``None`` and the detail screen hides the button.
    """

    server_id: UUID
    provider_key: str
    state: ServerLifecycleState
    provider_server_id: str | None
    stage: int  # 1 = warning, 2 = final confirmation
    idempotency_key: str
    warning: str
    confirm_callback: str  # stage 1 -> stage 2 (re-render with the same key)
    execute_callback: str  # stage 2 -> done -> DeleteCommandService.request
    back_callback: str  # servers.detail:<server-id>
    cancel_callback: str  # main.menu

    def render(self) -> str:
        """ASCII rendering for logs and the review UI."""
        lines = [
            f"Delete server {self.server_id} [{self.state.value}] @ {self.provider_key} "
            f"(stage {self.stage}/2)",
            f"  {self.warning}",
        ]
        return "\n".join(lines)


class DeleteConfirmationService:
    """Builds the delete confirmation screens (M08-009).

    Acceptance: **double confirmation and idempotent callback.** Two
    renders (stage 1 warning, stage 2 final confirmation) and one
    execution, all keyed by a single idempotency key:

    - ``screen(..., stage=1)`` - the warning; its confirm callback
      re-renders stage 2 with the same key.
    - ``screen(..., stage=2, idempotency_key=...)`` - the final
      confirmation; its execute callback is what the bot dispatches into
      ``DeleteCommandService.request(user_id, server_id, key)``.

    The view NEVER executes anything: the command re-checks ownership and
    state at request time, so a stale callback cannot delete a server that
    changed in the meantime. Ownership follows MyServersService (missing or
    foreign server -> uniform None).
    """

    _WARNING_1 = (
        "This permanently deletes the server. The server is stopped, the "
        "remaining usage is billed as a final charge, and nothing can be "
        "recovered."
    )
    _WARNING_2 = "FINAL CONFIRMATION: the server will be deleted now. This cannot be undone."

    def __init__(self, server_repo: ServerRepository, signing_key: str) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty")
        self._servers = server_repo
        self._signing_key = signing_key

    async def screen(
        self,
        user_id: UUID,
        server_id: UUID,
        *,
        stage: int = 1,
        idempotency_key: str | None = None,
    ) -> DeleteConfirmationView | None:
        """One confirmation stage for a server the user owns, or None.

        ``stage`` is 1 or 2; ``idempotency_key`` keeps the key stable across
        the two stages and across re-sends (a fresh UUID is minted when
        absent).
        """
        if stage not in (1, 2):
            raise ValueError("stage must be 1 or 2")
        server = await self._servers.get(server_id)
        if server is None or server.user_id != user_id:
            return None
        if server.state not in DELETE_PRECONDITION_STATES:
            # The delete command would reject this state: hide the button.
            return None

        key = idempotency_key if idempotency_key else uuid4().hex
        if not key.strip():
            key = uuid4().hex
        args = (str(server.id), key)
        return DeleteConfirmationView(
            server_id=server.id,
            provider_key=server.provider_key,
            state=server.state,
            provider_server_id=server.provider_server_id,
            stage=stage,
            idempotency_key=key,
            warning=self._WARNING_1 if stage == 1 else self._WARNING_2,
            confirm_callback=encode_callback(
                Callback(flow="delete", screen="confirm", args=args), self._signing_key
            ),
            execute_callback=encode_callback(
                Callback(flow="delete", screen="execute", args=args), self._signing_key
            ),
            back_callback=encode_callback(
                Callback(flow="servers", screen="detail", args=(str(server.id),)),
                self._signing_key,
            ),
            cancel_callback=encode_callback(
                Callback(flow="main", screen="menu"), self._signing_key
            ),
        )


# States the executor may find the server in when a claimed operation runs.
_DELETE_IN_PROGRESS_STATES: frozenset[ServerLifecycleState] = frozenset(
    {
        ServerLifecycleState.DELETE_REQUESTED,
        ServerLifecycleState.DELETING,
        ServerLifecycleState.DELETED,
    }
)


def delete_operation_key(server_id: UUID, idempotency_key: str) -> str:
    """Deterministic ledger key for one delete intent (unique per command key)."""
    return f"server-delete:{server_id}:{idempotency_key}"


@dataclass(frozen=True, slots=True)
class DeleteCommandResult:
    server: CloudServer
    replayed: bool  # True when a prior attempt with the same key already completed
    requeued: bool = False  # True when the attempt hit a retryable provider error


class DeleteExecutionResult(StrEnum):
    EXECUTED = "executed"
    REQUEUED = "requeued"


class DeleteOperationExecutor:
    """Executes a *claimed* SERVER_DELETE operation (the saga proper).

    Shared by the interactive command path (actor = the issuing user) and the
    worker path (actor = system, retrying re-queued operations). The operation
    key is the IdempotencyKey sent to the provider, so a re-send after an
    unresolved failure can never trigger a second physical deletion.
    """

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        final_charge: FinalChargeService,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        wallet_repo: WalletRepository,
        audit_repo: AuditRepository,
        waiter: ActionWaiter | None = None,
    ) -> None:
        self._ops = operation_repo
        self._servers = server_repo
        self._registry = provider_registry
        self._final_charge = final_charge
        self._hold_repo = hold_repo
        self._holds = hold_service
        self._wallets = wallet_repo
        self._audit = AuditTrail(audit_repo)
        self._waiter = waiter or ActionWaiter()

    async def execute(
        self,
        operation: Operation,
        *,
        actor_type: ActorType,
        actor_id: UUID | None = None,
    ) -> DeleteExecutionResult:
        """Run one claimed (IN_FLIGHT) delete operation; raises on permanent failure.

        M11-002: runs inside a span joined to the enqueuing request's trace.
        """
        from cloud_platform.observability.tracing import operation_span

        async with operation_span(
            "delete operation",
            traceparent=operation.traceparent,
            attributes={
                "cloud.operation.id": str(operation.id),
                "cloud.operation.type": operation.operation_type.value,
                "cloud.operation.attempts": operation.attempts,
                "cloud.provider": operation.provider_key,
            },
        ):
            return await self._execute_in_span(operation, actor_type=actor_type, actor_id=actor_id)

    async def _execute_in_span(
        self,
        operation: Operation,
        *,
        actor_type: ActorType,
        actor_id: UUID | None = None,
    ) -> DeleteExecutionResult:
        """The original execution body (runs inside the operation span)."""
        server = await self._servers.get(operation.resource_id)
        if server is None:
            await self._fail(operation, actor_type, actor_id, "server row missing")
        if server.state not in _DELETE_IN_PROGRESS_STATES:
            # The state moved since the intent was created (containment was
            # reversed to a live state, drift). Never delete against a
            # mismatched state.
            await self._fail(
                operation,
                actor_type,
                actor_id,
                f"server left the deletion states ({server.state.value}) before "
                "the provider deletion ran",
            )

        provider: CloudProvider | None = None
        if server.provider_server_id:
            try:
                provider = self._registry.get(server.provider_key)
            except KeyError:
                await self._fail(
                    operation,
                    actor_type,
                    actor_id,
                    f"unknown provider {server.provider_key!r}",
                )

        # Step 2: provider absence.
        if server.provider_server_id and provider is not None:
            if server.state is ServerLifecycleState.DELETE_REQUESTED:
                server.transition_to(ServerLifecycleState.DELETING)
                await self._servers.save(server)
            deleted = await self._ensure_provider_absent(
                operation, server, provider, actor_type, actor_id
            )
            if deleted is DeleteExecutionResult.REQUEUED:
                return DeleteExecutionResult.REQUEUED
        elif server.provider_server_id is None and server.state in (
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.DELETING,
        ):
            # No provider resource was ever created (failed before
            # provisioning): nothing to delete and no usage to bill. Return
            # any still-reserved creation hold to the wallet.
            await self._release_reservation(server)

        # Step 3: billing final + DELETED.
        deleted_at = server.deleted_at or datetime.now(UTC)
        if server.state in (
            ServerLifecycleState.DELETE_REQUESTED,
            ServerLifecycleState.DELETING,
        ):
            server.transition_to(ServerLifecycleState.DELETED)
            server.deleted_at = deleted_at
            await self._servers.save(server)

        charged_minor = 0
        charge_capped = False
        if server.provider_server_id:
            # A server that had a provider resource carries its final usage
            # segment (idempotent: a crash-replay settles nothing twice). A
            # server that never had one (failed before provisioning) has no
            # usage and its reservation was already returned above.
            try:
                result = await self._final_charge.charge_final(server, deleted_at)
                charged_minor = result.charged_minor
                charge_capped = result.capped
            except MissingSnapshotError as exc:
                # The resource is gone; the billing gap goes to the review
                # queue (M05 operations tooling) for manual reconciliation
                # instead of blocking the deletion fact.
                await self._audit.record_mutation(
                    actor_type=ActorType.SYSTEM,
                    actor_id=None,
                    action="billing.final_charge_failed",
                    resource_type="server",
                    resource_id=str(server.id),
                    reason=str(exc),
                    metadata={"deleted_at": deleted_at.isoformat()},
                )
                operation.fail(f"final charge failed: {exc}")
                await self._ops.save(operation)
                await self._audit.record_mutation(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="server.delete_failed",
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=str(server.id),
                    reason=f"final charge: no price snapshot ({exc})",
                    metadata={"operation_id": str(operation.id)},
                )
                raise DeleteOperationFailedError("final charge: no price snapshot") from exc

        # Step 4: deleted.
        operation.complete(
            {
                "provider_server_id": server.provider_server_id,
                "deleted_at": deleted_at.isoformat(),
                "charged_minor": charged_minor,
                "charge_capped": charge_capped,
            }
        )
        await self._ops.save(operation)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="server.deleted",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            reason="deletion confirmed at the provider; final usage settled",
            metadata={
                "operation_id": str(operation.id),
                "provider_server_id": server.provider_server_id or "",
                "deleted_at": deleted_at.isoformat(),
                "charged_minor": str(charged_minor),
                "charge_capped": str(charge_capped).lower(),
            },
        )
        return DeleteExecutionResult.EXECUTED

    async def _ensure_provider_absent(
        self,
        operation: Operation,
        server: CloudServer,
        provider: CloudProvider,
        actor_type: ActorType,
        actor_id: UUID | None,
    ) -> DeleteExecutionResult | None:
        """Delete at the provider (once per operation key) and verify absence.

        Returns REQUEUED when the run must be retried (retryable error or the
        provider still shows the server at the wait deadline); None when
        absence is confirmed.
        """
        assert server.provider_server_id is not None
        try:
            await provider.delete_server(
                server.provider_server_id, IdempotencyKey(operation.operation_key)
            )
        except ProviderNotFound:
            pass  # 404 on delete: the resource is already absent - success
        except ProviderError as exc:
            if classify_provider_error(exc) is ErrorClass.RETRYABLE:
                operation.requeue(str(exc))
                await self._ops.save(operation)
                await self._audit.record_mutation(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="server.delete_requeued",
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=str(server.id),
                    reason=str(exc),
                    metadata={"operation_id": str(operation.id)},
                )
                return DeleteExecutionResult.REQUEUED
            await self._fail(operation, actor_type, actor_id, str(exc))

        async def probe() -> WaitProbe:
            remote = await provider.get_server(server.provider_server_id or "")
            if remote is None:
                return WaitProbe(WaitState.COMPLETED, "provider reports no such server")
            return WaitProbe(
                WaitState.PENDING,
                f"provider still reports the server (status={remote.status or 'unknown'})",
            )

        wait = await self._waiter.wait_for(probe)
        if wait.outcome is WaitOutcome.TIMEOUT:
            # Ambiguity by deadline: the deletion may still finish. The SAME
            # operation re-runs later and re-verifies (404 is success), so
            # nothing can double-act.
            operation.requeue(
                f"provider still shows the server {wait.polls} polls after delete; "
                "re-verifying on the next attempt"
            )
            await self._ops.save(operation)
            await self._audit.record_mutation(
                actor_type=actor_type,
                actor_id=actor_id,
                action="server.delete_requeued",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server.id),
                reason=wait.detail or "absence wait timed out",
                metadata={
                    "operation_id": str(operation.id),
                    "polls": str(wait.polls),
                },
            )
            return DeleteExecutionResult.REQUEUED
        if wait.outcome is WaitOutcome.FAILED:
            await self._fail(
                operation, actor_type, actor_id, wait.detail or "provider deletion failed"
            )
        return None

    async def _release_reservation(self, server: CloudServer) -> None:
        """Return a still-reserved creation hold for a server without a resource."""
        if not server.idempotency_key:
            return
        wallet = await self._wallets.get(server.user_id)
        if wallet is None or wallet.id is None:
            return
        hold = await self._hold_repo.get_by_idempotency(
            wallet.id, f"server-create:{server.idempotency_key}"
        )
        if hold is not None and hold.status is HoldStatus.CREATED:
            assert hold.id is not None
            await self._holds.release_hold(
                wallet.id, hold.id, f"server-create:{server.idempotency_key}"
            )
            logger.info(
                "server %s: released reserved hold (no provider resource existed)", server.id
            )

    async def _fail(
        self,
        operation: Operation,
        actor_type: ActorType,
        actor_id: UUID | None,
        reason: str,
    ) -> NoReturn:
        operation.fail(reason)
        await self._ops.save(operation)
        await self._audit.record_mutation(
            actor_type=actor_type,
            actor_id=actor_id,
            action="server.delete_failed",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(operation.resource_id),
            reason=reason,
            metadata={"operation_id": str(operation.id)},
        )
        raise DeleteOperationFailedError(reason)


class DeleteCommandService:
    """User-facing delete command: request the deletion saga.

    Authorization is enforced here (the application layer), not the UI:
    - **Ownership** — the server must belong to the acting user; a server
      belonging to someone else (or not found) is indistinguishable.
    - **Capability** — the local state must allow a deletion.
    - **Idempotency** — one ledger operation per (server, command key); the
      operation key is the IdempotencyKey sent to the provider, so a retry
      of the same command can never double-act. A completed command is
      replayed without a provider call; an in-flight one is rejected; a
      failed one surfaces its recorded error.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        operation_repo: OperationRepository,
        provider_registry: ProviderRegistry,
        final_charge: FinalChargeService,
        hold_repo: HoldRepository,
        hold_service: HoldService,
        wallet_repo: WalletRepository,
        audit_repo: AuditRepository,
        executor: DeleteOperationExecutor | None = None,
    ) -> None:
        self._servers = server_repo
        self._ops = operation_repo
        self._audit = AuditTrail(audit_repo)
        self._executor = executor or DeleteOperationExecutor(
            operation_repo=operation_repo,
            server_repo=server_repo,
            provider_registry=provider_registry,
            final_charge=final_charge,
            hold_repo=hold_repo,
            hold_service=hold_service,
            wallet_repo=wallet_repo,
            audit_repo=audit_repo,
        )

    async def request(
        self, user_id: UUID, server_id: UUID, idempotency_key: str
    ) -> DeleteCommandResult:
        if not idempotency_key or not idempotency_key.strip():
            raise DeleteCommandError("idempotency_key is required")
        key = delete_operation_key(server_id, idempotency_key.strip())
        if len(key) > 128:
            raise DeleteCommandError("idempotency_key too long")

        server = await self._servers.get(server_id)
        if server is None or server.user_id != user_id:
            raise DeleteNotOwnerError("server not found")

        # Replays of an existing intent are resolved from the ledger BEFORE any
        # state gate: a completed deletion must stay idempotent even after
        # the server is DELETED.
        op = await self._ops.get_by_key(key)
        state_valid = server.state in DELETE_PRECONDITION_STATES
        if op is not None:
            if op.status is OperationStatus.COMPLETED:
                return DeleteCommandResult(
                    server=await self._servers.get(server_id) or server, replayed=True
                )
            if op.status is OperationStatus.FAILED:
                raise DeleteOperationFailedError(op.error or "delete operation failed")
            if op.status is OperationStatus.IN_FLIGHT:
                raise DeleteOperationInProgressError("delete operation is in progress")
            if not state_valid and op.attempts == 0:
                raise DeleteActionNotAllowedError(
                    f"delete not allowed in state {server.state.value}"
                )
        else:
            if not state_valid:
                raise DeleteActionNotAllowedError(
                    f"delete not allowed in state {server.state.value}"
                )
            server.transition_to(ServerLifecycleState.DELETE_REQUESTED)
            await self._servers.save(server)
            op = await self._ops.get_or_create(
                operation_key=key,
                operation_type=OperationType.SERVER_DELETE,
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=server_id,
                provider_key=server.provider_key,
            )
            await self._audit.record_mutation(
                actor_type=ActorType.USER,
                actor_id=user_id,
                action="server.delete_requested",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server_id),
                reason=f"delete requested (key={idempotency_key.strip()})",
                metadata={"operation_id": str(op.id)},
            )

        claimed = await self._ops.claim(op.id)
        if claimed is None:
            raise DeleteOperationInProgressError("delete operation is in progress")

        result = await self._executor.execute(claimed, actor_type=ActorType.USER, actor_id=user_id)
        return DeleteCommandResult(
            server=await self._servers.get(server_id) or server,
            replayed=False,
            requeued=result is DeleteExecutionResult.REQUEUED,
        )


class DeleteWorker:
    """Processes PENDING delete operations (crash recovery + retryable errors).

    The command path executes the saga inline; this worker picks up anything
    that was re-queued (retryable provider error, absence-wait timeout) or
    left PENDING by a crashed run, using the same executor and therefore the
    same operation keys.
    """

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        executor: DeleteOperationExecutor,
    ) -> None:
        self._ops = operation_repo
        self._executor = executor

    async def process_pending_deletes(self, limit: int = 10) -> dict[str, int]:
        """Claim and execute PENDING delete operations.

        Returns counts keyed by ``executed`` / ``requeued`` / ``failed`` /
        ``contended`` (lost the claim race).
        """
        counts = {"executed": 0, "requeued": 0, "failed": 0, "contended": 0}
        if limit <= 0:
            return counts
        for op in (await self._ops.list_pending([OperationType.SERVER_DELETE]))[:limit]:
            claimed = await self._ops.claim(op.id)
            if claimed is None:
                counts["contended"] += 1
                continue
            try:
                result = await self._executor.execute(
                    claimed, actor_type=ActorType.SYSTEM, actor_id=None
                )
            except DeleteOperationFailedError:
                counts["failed"] += 1
                continue
            counts["executed" if result is DeleteExecutionResult.EXECUTED else "requeued"] += 1
        return counts


class DeleteReconciliationOutcome(StrEnum):
    SKIPPED = "skipped"
    REQUEUED_IN_FLIGHT = "requeued_in_flight"
    RECREATED_OPERATION = "recreated_operation"
    FAILED = "failed"  # the server row is gone; the intent can never complete


def reconciled_delete_key(server_id: UUID) -> str:
    """Deterministic operation key for a deletion whose intent row was lost."""
    return f"server-delete:{server_id}:reconciled"


class DeleteTimeoutReconciler:
    """Reconciles deletion ambiguity: crashes anywhere in the saga (M07-008).

    The acceptance property: *404 is success; timeout ambiguity reconciles.*
    A deletion can be left ambiguous by a crash in any of its windows:

    - the operation was IN_FLIGHT (the worker died between the claim and the
      completion) - the provider may or may not have deleted the resource;
    - the command moved the server to DELETE_REQUESTED but died before the
      operation row was created.

    Both are resolved by re-entering the SAME saga with the SAME operation
    key, which is safe because:

    - ``provider.delete_server`` is re-sent with the original idempotency
      key, and a 404 (ProviderNotFound) on delete - the resource is already
      gone - is treated as success;
    - the absence check re-verifies against the provider's current state;
    - the final charge is idempotent under its own ledger keys, so the final
      usage segment is posted exactly once across all attempts.

    Re-queueing IN_FLIGHT operations to PENDING is the ledger's own
    retryable transition (``requeue``); nothing else mutates state here.
    """

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        audit_repo: AuditRepository,
        in_flight_timeout: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if in_flight_timeout <= timedelta(0):
            raise ValueError("in_flight_timeout must be positive")
        self._ops = operation_repo
        self._servers = server_repo
        self._audit = AuditTrail(audit_repo)
        self._in_flight_timeout = in_flight_timeout
        self._now = clock or (lambda: datetime.now(UTC))

    async def reconcile(self) -> dict[DeleteReconciliationOutcome, int]:
        """Scan for ambiguous deletions and resolve each safely."""
        counts: dict[DeleteReconciliationOutcome, int] = {}

        def bump(outcome: DeleteReconciliationOutcome) -> None:
            counts[outcome] = counts.get(outcome, 0) + 1
            # M11-006: the reconciliation drift alert feed.
            metrics.record_reconciliation("delete", outcome.value)

        # 1) IN_FLIGHT delete operations past the timeout (ambiguous window).
        for op in await self._ops.list_in_flight(OperationType.SERVER_DELETE):
            server = await self._servers.get(op.resource_id)
            if server is None:
                op.fail("server row missing during delete-timeout reconciliation")
                await self._ops.save(op)
                bump(DeleteReconciliationOutcome.FAILED)
                continue
            age = None if op.updated_at is None else self._now() - op.updated_at
            if age is not None and age < self._in_flight_timeout:
                bump(DeleteReconciliationOutcome.SKIPPED)
                continue
            op.requeue(
                "in-flight timeout: re-entering the deletion saga with the "
                "same operation key (404 on delete is success)"
            )
            await self._ops.save(op)
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="server.delete_reconciled",
                resource_type="server",
                resource_id=str(server.id),
                reason=f"IN_FLIGHT delete operation {op.operation_key} re-queued (age {age})",
                metadata={
                    "operation_id": str(op.id),
                    "attempts": str(op.attempts),
                },
            )
            bump(DeleteReconciliationOutcome.REQUEUED_IN_FLIGHT)

        # 2) Servers stuck in DELETE_REQUESTED without an operation row (crash
        # between the command's state save and the op create). Recreating the
        # operation is safe: a PENDING operation has never called the
        # provider, and the deletion re-runs the same saga. A server that
        # already has ANY delete operation (PENDING / IN_FLIGHT / FAILED) is
        # left to the worker / the manual-retry tooling - a second
        # operation would only double-run a replay-safe saga.
        delete_ops = (
            await self._ops.list_pending([OperationType.SERVER_DELETE])
            + await self._ops.list_in_flight(OperationType.SERVER_DELETE)
            + await self._ops.list_failed(operation_types=[OperationType.SERVER_DELETE])
        )
        for server in await self._servers.list_deletion_in_progress():
            if server.state is not ServerLifecycleState.DELETE_REQUESTED:
                continue  # DELETING implies the operation exists (handled in 1)
            if any(op.resource_id == server.id for op in delete_ops):
                continue
            await self._ops.get_or_create(
                operation_key=reconciled_delete_key(server.id),
                operation_type=OperationType.SERVER_DELETE,
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=server.id,
                provider_key=server.provider_key,
            )
            await self._audit.record_mutation(
                actor_type=ActorType.SYSTEM,
                actor_id=None,
                action="server.delete_reconciled",
                resource_type="server",
                resource_id=str(server.id),
                reason="DELETE_REQUESTED server had no delete operation; "
                "recreated with the reconciled key",
                metadata={},
            )
            bump(DeleteReconciliationOutcome.RECREATED_OPERATION)

        return counts


# ---------------------------------------------------------------------------
# Rebuild flow (M13-002)
# ---------------------------------------------------------------------------

#: Rebuild (re-image) is allowed from steady states only: the disk is wiped,
#: so a server mid-transition must not be re-imaged on top of a saga.
REBUILD_PRECONDITION_STATES: frozenset[ServerLifecycleState] = frozenset(
    {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
)


class RebuildCommandError(Exception):
    """Base class for rebuild command errors."""


class RebuildConfirmationRequiredError(RebuildCommandError):
    """Rebuild is destructive and was requested without explicit confirmation."""


class RebuildNotOwnerError(RebuildCommandError):
    """The acting user does not own the server (indistinguishable from missing)."""


class RebuildActionNotAllowedError(RebuildCommandError):
    """The server's current state does not allow a rebuild."""


class RebuildOperationInProgressError(RebuildCommandError):
    """A rebuild with this key is already in flight."""


class RebuildOperationFailedError(RebuildCommandError):
    """A prior attempt with the same key failed permanently."""


def rebuild_operation_key(server_id: UUID, idempotency_key: str) -> str:
    """Deterministic ledger key for one rebuild intent (unique per command key)."""
    return f"server-rebuild:{server_id}:{idempotency_key}"


@dataclass(frozen=True, slots=True)
class RebuildCommandResult:
    server: CloudServer
    replayed: bool  # True when a prior attempt with the same key already completed
    requeued: bool = False  # True when the attempt hit a retryable provider error


class RebuildExecutionResult(StrEnum):
    EXECUTED = "executed"
    REQUEUED = "requeued"


class RebuildOperationExecutor:
    """Executes a *claimed* SERVER_REBUILD operation against the provider.

    Credential handling is safe by construction: the ONLY thing ever sent
    to the provider is an image REFERENCE. No password material exists in
    this flow; access after the wipe is re-established through the user's
    registered SSH keys (M13-001), which are audited BY COUNT ONLY - never
    by material.

    The target image is resolved per attempt through ``image_lookup`` so
    every retry of the same operation uses the same deterministic target.
    """

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
        image_lookup: Callable[[UUID], Any] | None = None,
    ) -> None:
        self._ops = operation_repo
        self._servers = server_repo
        self._registry = provider_registry
        self._audit = AuditTrail(audit_repo)
        self._image_lookup = image_lookup

    async def execute(
        self,
        operation: Operation,
        *,
        actor_type: ActorType,
        actor_id: UUID | None = None,
        image_id: str | None = None,
    ) -> RebuildExecutionResult:
        """Run one claimed (IN_FLIGHT) rebuild; raises on permanent failure."""
        from cloud_platform.observability.tracing import operation_span

        async with operation_span(
            "rebuild operation",
            traceparent=operation.traceparent,
            attributes={
                "cloud.operation.id": str(operation.id),
                "cloud.operation.type": operation.operation_type.value,
                "cloud.operation.attempts": operation.attempts,
                "cloud.provider": operation.provider_key,
            },
        ):
            return await self._execute_in_span(
                operation, actor_type=actor_type, actor_id=actor_id, image_id=image_id
            )

    async def _execute_in_span(
        self,
        operation: Operation,
        *,
        actor_type: ActorType,
        actor_id: UUID | None = None,
        image_id: str | None = None,
    ) -> RebuildExecutionResult:
        if operation.operation_type is not OperationType.SERVER_REBUILD:
            await self._fail(operation, actor_type, actor_id, "not a rebuild operation")
        server = await self._servers.get(operation.resource_id)
        if server is None:
            await self._fail(operation, actor_type, actor_id, "server row missing")
        if not server.provider_server_id:
            await self._fail(operation, actor_type, actor_id, "server has no provider resource id")
        if server.state not in REBUILD_PRECONDITION_STATES:
            # The state moved since the intent was created. Never re-image a
            # server that is mid-delete / mid-provision.
            await self._fail(
                operation,
                actor_type,
                actor_id,
                f"server left the expected state ({server.state.value}) before "
                "the rebuild was executed",
            )
        try:
            provider: CloudProvider = self._registry.get(server.provider_key)
        except KeyError:
            await self._fail(
                operation, actor_type, actor_id, f"unknown provider {server.provider_key!r}"
            )
        rebuild = rebuild_support_of(provider)
        if rebuild is None:
            await self._fail(operation, actor_type, actor_id, "provider lacks rebuild support")

        target_image = image_id
        if target_image is None and self._image_lookup is not None:
            resolved = self._image_lookup(server.id)
            target_image = await resolved if hasattr(resolved, "__await__") else resolved
        if not target_image or not str(target_image).strip():
            await self._fail(operation, actor_type, actor_id, "no target image for rebuild")

        try:
            status = await rebuild(server.provider_server_id, str(target_image))
        except ProviderError as exc:
            if classify_provider_error(exc) is ErrorClass.RETRYABLE:
                operation.requeue(str(exc))
                await self._ops.save(operation)
                await self._audit.record_mutation(
                    actor_type=actor_type,
                    action="server.rebuild_requeued",
                    resource_type=RESOURCE_TYPE_SERVER,
                    resource_id=str(server.id),
                    actor_id=actor_id,
                    reason=str(exc),
                    metadata={"operation_id": str(operation.id)},
                )
                return RebuildExecutionResult.REQUEUED
            await self._fail(operation, actor_type, actor_id, str(exc))

        operation.complete(
            {"action": "rebuild", "image": str(target_image), "provider_status": str(status)}
        )
        await self._ops.save(operation)
        await self._audit.record_mutation(
            actor_type=actor_type,
            action="server.rebuilt",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(server.id),
            actor_id=actor_id,
            reason=f"user-confirmed rebuild with image {target_image}",
            metadata={
                "operation_id": str(operation.id),
                "image_id": str(target_image),
                "provider_status": str(status),
            },
        )
        return RebuildExecutionResult.EXECUTED

    async def _fail(
        self,
        operation: Operation,
        actor_type: ActorType,
        actor_id: UUID | None,
        reason: str,
    ) -> NoReturn:
        operation.fail(reason)
        await self._ops.save(operation)
        await self._audit.record_mutation(
            actor_type=actor_type,
            action="server.rebuild_failed",
            resource_type=RESOURCE_TYPE_SERVER,
            resource_id=str(operation.resource_id),
            actor_id=actor_id,
            reason=reason,
            metadata={"operation_id": str(operation.id)},
        )
        raise RebuildOperationFailedError(reason)


class RebuildCommandService:
    """User-facing rebuild command: request a confirmed re-image.

    Acceptance: **confirmation and credential handling safe.**

    - **Confirmation** - rebuild wipes the server's disk; the request MUST
      carry ``confirmed=True`` (the UI's final-confirmation step). An
      unconfirmed call is rejected before ANY lookup happens.
    - **Credential safety** - the command surface accepts an image reference
      only. There is no password parameter anywhere in the flow; post-rebuild
      access goes through the user's registered SSH keys, which are audited
      by count only. The audit trail records who rebuilt what, when, with
      which image, and under which ledger key.
    - **Ownership** - enforced here: another user's server is "not found".
    - **Idempotency** - one ledger operation per (server, command key); the
      operation key is sent to the provider, retries reuse it, completed
      commands replay without a provider call.
    """

    def __init__(
        self,
        *,
        server_repo: ServerRepository,
        operation_repo: OperationRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
        executor: RebuildOperationExecutor | None = None,
    ) -> None:
        self._servers = server_repo
        self._ops = operation_repo
        self._audit = AuditTrail(audit_repo)
        self._executor = executor or RebuildOperationExecutor(
            operation_repo=operation_repo,
            server_repo=server_repo,
            provider_registry=provider_registry,
            audit_repo=audit_repo,
        )

    async def request(
        self,
        user_id: UUID,
        server_id: UUID,
        image_id: str,
        idempotency_key: str,
        *,
        confirmed: bool,
    ) -> RebuildCommandResult:
        if not confirmed:
            # Gate BEFORE any data access: confirmation is part of the
            # command's contract, not a UI nicety.
            raise RebuildConfirmationRequiredError(
                "rebuild wipes the server; pass confirmed=True (final UI step)"
            )
        if not idempotency_key or not idempotency_key.strip():
            raise RebuildCommandError("idempotency_key is required")
        stripped_key = idempotency_key.strip()
        if len(stripped_key) > 128:
            raise RebuildCommandError("idempotency_key too long")
        image = (image_id or "").strip()
        if not image or len(image) > 128:
            raise RebuildCommandError("a non-empty image reference (max 128 chars) is required")

        server = await self._servers.get(server_id)
        if server is None or server.user_id != user_id:
            raise RebuildNotOwnerError("server not found")

        # Replay resolution BEFORE state gates (same convention as delete).
        op = await self._ops.get_by_key(rebuild_operation_key(server_id, stripped_key))
        state_valid = server.state in REBUILD_PRECONDITION_STATES
        if op is not None:
            if op.status is OperationStatus.COMPLETED:
                return RebuildCommandResult(
                    server=await self._servers.get(server_id) or server, replayed=True
                )
            if op.status is OperationStatus.FAILED:
                raise RebuildOperationFailedError(op.error or "rebuild operation failed")
            if op.status is OperationStatus.IN_FLIGHT:
                raise RebuildOperationInProgressError("rebuild operation is in progress")
            if not state_valid and op.attempts == 0:
                raise RebuildActionNotAllowedError(
                    f"rebuild not allowed in state {server.state.value}"
                )
        else:
            if not state_valid:
                raise RebuildActionNotAllowedError(
                    f"rebuild not allowed in state {server.state.value}"
                )
            op = await self._ops.get_or_create(
                operation_key=rebuild_operation_key(server_id, stripped_key),
                operation_type=OperationType.SERVER_REBUILD,
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=server_id,
                provider_key=server.provider_key,
            )
            await self._audit.record_mutation(
                actor_type=ActorType.USER,
                actor_id=user_id,
                action="server.rebuild_requested",
                resource_type=RESOURCE_TYPE_SERVER,
                resource_id=str(server_id),
                reason=f"confirmed rebuild requested (key={stripped_key})",
                metadata={"operation_id": str(op.id), "image_id": image},
            )

        claimed = await self._ops.claim(op.id)
        if claimed is None:
            raise RebuildOperationInProgressError("rebuild operation is in progress")

        result = await self._executor.execute(
            claimed, actor_type=ActorType.USER, actor_id=user_id, image_id=image
        )
        return RebuildCommandResult(
            server=await self._servers.get(server_id) or server,
            replayed=False,
            requeued=result is RebuildExecutionResult.REQUEUED,
        )


class RebuildWorker:
    """Processes PENDING rebuild operations (crash recovery + retries).

    Uses the SAME executor and therefore the same operation keys as the
    interactive command path; the target image comes from the injected
    ``image_lookup`` so every attempt of one intent resolves identically.
    """

    def __init__(
        self,
        *,
        operation_repo: OperationRepository,
        server_repo: ServerRepository,
        provider_registry: ProviderRegistry,
        audit_repo: AuditRepository,
        image_lookup: Callable[[UUID], Any],
    ) -> None:
        self._ops = operation_repo
        self._executor = RebuildOperationExecutor(
            operation_repo=operation_repo,
            server_repo=server_repo,
            provider_registry=provider_registry,
            audit_repo=audit_repo,
            image_lookup=image_lookup,
        )

    async def process_pending(self) -> dict[str, int]:
        """Run every PENDING rebuild once; returns outcome counts."""
        counts = {"executed": 0, "requeued": 0}
        for op in await self._ops.list_pending([OperationType.SERVER_REBUILD]):
            claimed = await self._ops.claim(op.id)
            if claimed is None:
                continue
            result = await self._executor.execute(claimed, actor_type=ActorType.SYSTEM)
            counts["executed" if result is RebuildExecutionResult.EXECUTED else "requeued"] += 1
        return counts
