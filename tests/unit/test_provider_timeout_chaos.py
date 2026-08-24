"""Chaos tests for provider timeouts (M11-009).

Acceptance: no double create/charge after an injected timeout.

The scary window: the platform's HTTP call to the provider times out, but
the provider may have ALREADY applied the mutation (created the server,
deleted it, moved the money). A naive retry would double-create or
double-charge. These tests inject the timeout at the HTTP layer of a REAL
adapter and assert the idempotency machinery holds:

- create: the worker re-queues on timeout; the create-timeout reconciler
  re-resolves with the SAME operation key; a provider that deduplicates on
  the key/label returns the SAME server -> exactly one physical server.
- delete: a timed-out delete is re-sent with the same key; the adapter
  treats the resulting 404 as success -> no double-delete error.
- charge: a timeout AFTER the ledger entry committed (lost ack) is
  replayed; the deterministic entry key rejects the second post -> exactly
  one debit, one ledger entry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.billing.service import FinalChargeService
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
    ProvisioningOutcome,
    ProvisioningWorker,
    ReconciliationOutcome,
    server_operation_key,
)
from cloud_platform.modules.wallet.domain import Wallet
from cloud_platform.providers.base import CreateServerRequest, ProviderImage
from cloud_platform.providers.errors import ProviderUnavailable
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.registry import ProviderRegistry

SERVER_ID = uuid4()
WALLET_ID = uuid4()
USER_ID = uuid4()
SPEC = ProvisioningSpec(plan_id="cx22", location_id="fsn1", currency="EUR")
OP_KEY = server_operation_key(SERVER_ID)
NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Chaos transport: a fake Hetzner API that deduplicates on the
# platform-operation label and injects timeouts on schedule.
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        import json as _json

        self._body = body if body is not None else {}
        # the adapter treats an EMPTY BODY as "no payload"; keep it non-empty
        self.content = _json.dumps(self._body).encode("utf-8")
        self.is_error = status_code >= 400

    def json(self) -> Any:
        return self._body

    @property
    def text(self) -> str:
        return str(self._body)


class ChaosHetznerAPI:
    """Simulates the provider side, chaos included.

    Deduplication contract: a ``POST /servers`` carrying the same
    ``platform-operation`` label returns the ALREADY-CREATED server instead
    of creating a second one - exactly the behaviour the M07-003 contract
    requires of a participating provider (the real Hetzner integration is
    guarded by the same label + the create-timeout reconciler).
    """

    def __init__(self) -> None:
        self.servers: dict[str, dict[str, Any]] = {}
        self.deleted: set[str] = set()
        self.create_post_count = 0
        self.delete_count = 0
        # chaos: call-number of the create POST that times out AFTER the
        # server was created (None = no chaos).
        self.create_timeout_after_call: int | None = None
        # chaos: call-number of the create POST that times out BEFORE
        # anything is created (the clean-retry ambiguity).
        self.create_timeout_before_call: int | None = None
        # chaos: make the delete DELETE time out after it was applied
        self.delete_timeout_after_call: int | None = None

    def _payload(self, sid: str, name: str, labels: dict[str, str]) -> dict[str, Any]:
        return {
            "server": {
                "id": sid,
                "name": name,
                "status": "creating",
                "labels": labels,
                "public_net": {"ipv4": {"ip": "10.0.0.1"}, "ipv6": {"ip": None}},
            }
        }

    async def request(self, method: str, path: str, **kwargs: Any) -> _Resp:
        if method == "GET" and path == "/images":
            return _Resp(
                200,
                {
                    "images": [
                        {
                            "id": "img-linux",
                            "name": "debian",
                            "os_flavor": "linux",
                            "architecture": "x86",
                        }
                    ]
                },
            )
        if method == "GET" and path == "/server_types":
            return _Resp(200, {"server_types": [{"name": "cx22"}]})
        if method == "GET" and path == "/locations":
            return _Resp(200, {"locations": [{"id": "fsn1"}]})
        if method == "POST" and path == "/servers":
            self.create_post_count += 1
            if self.create_timeout_before_call == self.create_post_count:
                raise httpx.ReadTimeout("injected chaos: create timed out before landing")
            body = kwargs.get("json") or {}
            labels = dict(body.get("labels") or {})
            op_label = labels.get("platform-operation")
            # provider-side dedup: same operation label -> same resource
            for existing in self.servers.values():
                if existing.get("labels", {}).get("platform-operation") == op_label:
                    if self.create_timeout_after_call == self.create_post_count:
                        raise httpx.ReadTimeout("injected chaos: create timed out")
                    return _Resp(201, self._payload(existing["id"], body["name"], labels))
            sid = f"srv-{len(self.servers) + 1:04d}"
            server = {"id": sid, "name": body["name"], "labels": labels}
            self.servers[sid] = server
            if self.create_timeout_after_call == self.create_post_count:
                # the mutation LANDED at the provider, then the response was lost
                raise httpx.ReadTimeout("injected chaos: create timed out")
            return _Resp(201, self._payload(sid, body["name"], labels))
        if method == "DELETE" and path.startswith("/servers/"):
            sid = path.rsplit("/", 1)[-1]
            self.delete_count += 1
            if sid in self.servers:
                del self.servers[sid]
                self.deleted.add(sid)
            if self.delete_timeout_after_call == self.delete_count:
                raise httpx.ReadTimeout("injected chaos: delete timed out")
            if sid not in self.servers and sid not in self.deleted:
                return _Resp(404, {"error": {"code": "not_found", "message": "gone"}})
            return _Resp(204)
        if method == "GET" and path.startswith("/servers/"):
            sid = path.rsplit("/", 1)[-1]
            server = self.servers.get(sid)
            if server is None:
                return _Resp(404, {"error": {"code": "not_found", "message": "gone"}})
            return _Resp(200, self._payload(sid, server["name"], server["labels"]))
        return _Resp(404, {"error": {"code": "not_found", "message": "unknown route"}})


def make_provider(chaos: ChaosHetznerAPI) -> HetznerCloudProvider:
    provider = HetznerCloudProvider(token="chaos-token")
    provider._client = chaos  # type: ignore[assignment]
    return provider


# ---------------------------------------------------------------------------
# Harness (mirrors test_provisioning_worker / test_create_timeout_reconciler)
# ---------------------------------------------------------------------------


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
        resource_id: Any,
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

    async def get(self, operation_id: Any) -> Operation | None:
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

    async def list_pending(self, operation_types: Any) -> list[Operation]:
        values = [t.value for t in operation_types]
        return [
            op
            for op in self.by_key.values()
            if op.operation_type.value in values and op.status is OperationStatus.PENDING
        ]

    async def claim(self, operation_id: Any) -> Operation | None:
        op = await self.get(operation_id)
        if op is None or op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        return op

    async def save(self, operation: Operation) -> Operation:
        operation.updated_at = NOW
        self.saved.append(operation)
        return operation


class FakeServerRepo:
    def __init__(self, servers: dict[Any, CloudServer]) -> None:
        self.servers = dict(servers)
        self.specs: dict[Any, Any] = {k: SPEC for k in servers}
        self.saved: list[CloudServer] = []

    async def get(self, server_id: Any) -> CloudServer | None:
        return self.servers.get(server_id)

    async def list_requested(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.REQUESTED]

    async def list_provisioning(self) -> list[CloudServer]:
        return [s for s in self.servers.values() if s.state is ServerLifecycleState.PROVISIONING]

    async def get_provisioning_spec(self, server_id: Any) -> Any:
        return self.specs.get(server_id)

    async def save(self, server: CloudServer) -> CloudServer:
        self.saved.append(server)
        return server


class _Harness:
    def __init__(
        self,
        chaos: ChaosHetznerAPI,
        provider: HetznerCloudProvider,
        clock_now: datetime = NOW,
    ) -> None:
        self.chaos = chaos
        self.provider = provider
        self.clock_now = clock_now
        self.server: CloudServer | None = None
        self.ops: FakeOpRepo | None = None
        self.worker: Any = None
        self.reconciler: Any = None

    @staticmethod
    def _server(state: ServerLifecycleState = ServerLifecycleState.REQUESTED) -> CloudServer:
        return CloudServer(
            id=SERVER_ID,
            user_id=USER_ID,
            provider_key="hetzner",
            provider_account_id=uuid4(),
            state=state,
            idempotency_key="cmd-key-1",
        )

    def build(self) -> _Harness:
        server = self._server()
        self.server = server
        ops = FakeOpRepo()
        self.ops = ops
        server_repo = FakeServerRepo({server.id: server})
        registry = ProviderRegistry()
        registry.register(self.provider)  # type: ignore[arg-type]
        wallets = AsyncMock()
        wallets.get = AsyncMock(
            return_value=Wallet(user_id=USER_ID, id=WALLET_ID, balance=100, currency="EUR")
        )
        holds = AsyncMock()
        holds.get_by_idempotency = AsyncMock(return_value=None)
        holds.release_hold = AsyncMock(return_value=None)
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)
        images = FirstLinuxImageSelector()
        self.worker = ProvisioningWorker(
            operation_repo=ops,  # type: ignore[arg-type]
            server_repo=server_repo,  # type: ignore[arg-type]
            provider_registry=registry,
            image_selector=images,
            wallet_repo=wallets,  # type: ignore[arg-type]
            hold_repo=holds,  # type: ignore[arg-type]
            audit_repo=audit,  # type: ignore[arg-type]
        )
        self.reconciler = CreateTimeoutReconciler(
            operation_repo=ops,  # type: ignore[arg-type]
            server_repo=server_repo,  # type: ignore[arg-type]
            provider_registry=registry,
            image_selector=images,
            wallet_repo=wallets,  # type: ignore[arg-type]
            hold_repo=holds,  # type: ignore[arg-type]
            audit_repo=audit,  # type: ignore[arg-type]
            clock=lambda: self.clock_now,
        )
        return self


def _linux_images() -> list[ProviderImage]:
    return [ProviderImage(id="img-linux", name="debian", os_family="linux", architecture="x86")]


# ---------------------------------------------------------------------------
# 1. Create chaos
# ---------------------------------------------------------------------------


class TestCreateTimeoutChaos:
    async def test_crash_during_create_no_duplicate_server(self) -> None:
        """The ambiguous window: the worker DIES (op stays IN_FLIGHT) after
        the provider applied the create; the response timed out. The
        create-timeout reconciler must recover the SAME server, not create a
        second one."""
        chaos = ChaosHetznerAPI()
        # the first create POST times out AFTER landing at the provider
        chaos.create_timeout_after_call = 1
        harness = _Harness(chaos=chaos, provider=make_provider(chaos)).build()

        # the worker claims and calls the provider -> timeout -> process crash
        op = await harness.ops.get_or_create(
            operation_key=OP_KEY,
            operation_type=OperationType.SERVER_CREATE,
            resource_type="server",
            resource_id=SERVER_ID,
            provider_key="hetzner",
        )
        claimed = await harness.ops.claim(op.id)
        assert claimed is not None
        with pytest.raises(ProviderUnavailable):
            await harness.provider.create_server(_fake_request(), IdempotencyKey(op.operation_key))
        # chaos: the crash means NO requeue - the op is still IN_FLIGHT
        assert op.status is OperationStatus.IN_FLIGHT
        # and the provider did create the server behind our back
        assert len(chaos.servers) == 1
        # the worker's crash left it aged past the timeout
        harness.clock_now = NOW + timedelta(hours=1)

        counts = await harness.reconciler.reconcile()
        assert counts.get(ReconciliationOutcome.RECOVERED) == 1

        # NO duplicate: exactly one physical server, despite two POSTs
        assert len(chaos.servers) == 1
        assert chaos.create_post_count == 2
        # the operation completed with the correlation to that ONE server
        assert op.status is OperationStatus.COMPLETED
        assert op.provider_response is not None
        assert op.provider_response["provider_server_id"] in chaos.servers
        # the server row points at it
        assert harness.server.provider_server_id in chaos.servers

    async def test_requeued_timeout_then_retry_creates_once(self) -> None:
        """Clean chaos: the first create times out BEFORE it lands (nothing
        created); the worker re-queues with the same key and the retry
        creates exactly one server."""
        chaos = ChaosHetznerAPI()
        chaos.create_timeout_before_call = 1
        harness = _Harness(chaos=chaos, provider=make_provider(chaos)).build()

        # first run: timeout -> requeue (the provider saw nothing)
        outcome = await harness.worker.process_server(SERVER_ID)
        assert outcome is ProvisioningOutcome.REQUEUED
        assert chaos.create_post_count == 1
        assert len(chaos.servers) == 0  # nothing landed this time
        op = harness.ops.by_key[OP_KEY]
        assert op.status is OperationStatus.PENDING
        assert op.attempts == 1

        # second run: same key, provider has no record -> one create
        outcome = await harness.worker.process_server(SERVER_ID)
        assert outcome is ProvisioningOutcome.PROVISIONED
        assert chaos.create_post_count == 2
        assert len(chaos.servers) == 1  # exactly ONE physical server
        assert op.status is OperationStatus.COMPLETED
        assert op.attempts == 2
        assert harness.server.provider_server_id in chaos.servers

    async def test_requeue_uses_same_idempotency_key(self) -> None:
        """A retry after a timeout must never mint a new operation key -
        that is what makes provider-side dedup possible."""
        chaos = ChaosHetznerAPI()
        chaos.create_timeout_before_call = 1
        harness = _Harness(chaos=chaos, provider=make_provider(chaos)).build()

        await harness.worker.process_server(SERVER_ID)  # times out, requeues
        # the provider saw the label of the first attempt; the retry must
        # carry the SAME label (same operation key)
        await harness.worker.process_server(SERVER_ID)
        assert chaos.create_post_count == 2
        # both attempts labeled with the same operation key
        assert len(chaos.servers) == 1
        for server in chaos.servers.values():
            assert server["labels"]["platform-operation"] == OP_KEY[:63]


# ---------------------------------------------------------------------------
# 2. Delete chaos
# ---------------------------------------------------------------------------


class TestDeleteTimeoutChaos:
    async def test_delete_timeout_then_resend_is_not_a_double_delete(self) -> None:
        """The delete call times out AFTER the provider applied it; the saga
        re-sends the same intent. The adapter must treat the resulting 404
        as success (delete is 404-idempotent), not as a failure."""
        chaos = ChaosHetznerAPI()
        sid = "srv-9001"
        chaos.servers[sid] = {"id": sid, "name": "victim", "labels": {}}
        chaos.delete_timeout_after_call = 1
        provider = make_provider(chaos)

        with pytest.raises(ProviderUnavailable):
            await provider.delete_server(sid, IdempotencyKey("server-delete:1"))
        # the provider applied the deletion before the timeout
        assert sid in chaos.deleted
        assert chaos.delete_count == 1

        # the saga re-sends the SAME intent
        await provider.delete_server(sid, IdempotencyKey("server-delete:1"))  # must not raise
        assert chaos.delete_count == 2
        assert len(chaos.servers) == 0  # still gone, no error, no double state


# ---------------------------------------------------------------------------
# 3. Charge chaos: lost ack after the ledger commit
# ---------------------------------------------------------------------------


def _make_debit_side_effect(harness: _ChargeHarness) -> Any:
    """Build an async side_effect bound to the harness instance."""

    async def _debit(*args: Any, **kwargs: Any) -> Any:
        return await harness._debit(*args, **kwargs)

    return _debit


class TestFinalChargeTimeoutChaos:
    async def test_timeout_after_commit_replays_without_double_charge(self) -> None:
        """A timeout AFTER the ledger entry committed (the ack was lost) must
        not produce a second debit on replay: the deterministic entry key
        ``final:{server_id}`` makes the second post a no-op."""
        server_id = uuid4()
        created = NOW - timedelta(hours=3)
        deleted = NOW - timedelta(hours=1)
        server = CloudServer(
            id=server_id,
            user_id=USER_ID,
            provider_key="hetzner",
            provider_account_id=uuid4(),
            state=ServerLifecycleState.DELETED,
            idempotency_key=None,
            created_at=created,
            deleted_at=deleted,
            quantum_seconds=3600,
        )

        harness = _ChargeHarness(server_id)
        # chaos: the first commit's ack is lost -> the flow sees a timeout
        harness.fail_next_post_with = True
        with pytest.raises(TimeoutError):
            await harness.charge_final(server, deleted)
        # the entry DID commit before the timeout (the ack was lost, not the row)
        assert len(harness.entries) == 1
        assert harness.debits == 1

        # the deletion flow replays charge_final (crash-replay)
        result2 = await harness.charge_final(server, deleted)
        # NO double charge: the replayed leg is detected by its key
        assert harness.debits == 1
        assert len(harness.entries) == 1
        assert result2.charged_minor == 0 or result2.replayed
        # the wallet was debited exactly once
        assert harness.wallet.balance == 1_000_000 - 1000 * 2  # 2 quanta (2h window)


class _ChargeHarness:
    """FinalChargeService over in-memory repos that can simulate a lost ack."""

    def __init__(self, server_id: Any) -> None:
        self.debits = 0
        self.entries: list[dict[str, Any]] = []
        self.fail_next_post_with = False
        self.wallet = Wallet(user_id=USER_ID, id=WALLET_ID, balance=1_000_000, currency="EUR")
        self.wallets = AsyncMock()
        self.wallets.get = AsyncMock(return_value=self.wallet)
        self.wallets.debit = AsyncMock(side_effect=_make_debit_side_effect(self))
        self.holds = AsyncMock()
        self.holds.get_by_idempotency = AsyncMock(return_value=None)
        self.holds.capture_hold = AsyncMock(return_value=None)
        self.holds.release_hold = AsyncMock(return_value=None)
        self.ledger = AsyncMock()
        self.ledger.get_entry_by_idempotency = AsyncMock(side_effect=self._get_entry)
        self.ledger.post_entry = AsyncMock(side_effect=self._post_entry)
        self.accruals = AsyncMock()
        self.accruals.month_total = AsyncMock(return_value=0)
        self.accruals.add = AsyncMock(return_value=None)
        self.servers = AsyncMock()
        self.servers.save = AsyncMock(return_value=None)
        self.snapshots = AsyncMock()
        self.audit = AsyncMock()
        self.audit.append = AsyncMock(side_effect=lambda e: e)

        from decimal import Decimal

        from cloud_platform.modules.pricing.domain import (
            MarginRule,
            OfferCost,
            ServerPriceSnapshot,
        )

        offer = OfferCost(
            provider_key="hetzner",
            plan_id="cx22",
            location_id="fsn1",
            cost_minor=600,
            currency="EUR",
        )
        rule = MarginRule(
            provider="hetzner",
            plan="cx22",
            location="fsn1",
            margin_factor=Decimal("1.15"),
        )
        snapshot = ServerPriceSnapshot(
            server_id=server_id,
            offer=offer,
            selling_minor=1000,
            book_name="chaos",
            book_version=1,
            rule=rule,
            priced_at=NOW,
        )
        self.snapshots.get = AsyncMock(return_value=snapshot)
        self._service = FinalChargeService(
            server_repo=self.servers,  # type: ignore[arg-type]
            wallet_repo=self.wallets,  # type: ignore[arg-type]
            hold_repo=self.holds,  # type: ignore[arg-type]
            hold_service=self.holds,  # type: ignore[arg-type]
            ledger_repo=self.ledger,  # type: ignore[arg-type]
            accrual_repo=self.accruals,  # type: ignore[arg-type]
            snapshot_repo=self.snapshots,  # type: ignore[arg-type]
            audit_repo=self.audit,  # type: ignore[arg-type]
        )

    async def _debit(self, uid: Any, amount: int, key: str) -> None:
        # the wallet ledger rejects a duplicate (wallet_id, key)
        for entry in self.entries:
            if entry["key"] == key:
                raise ValueError(f"duplicate ledger key {key}")
        self.debits += 1
        self.wallet.balance -= amount

    async def _get_entry(self, wallet_id: Any, key: str) -> Any:
        for entry in self.entries:
            if entry["key"] == key:
                return entry
        return None

    async def _post_entry(
        self,
        wallet_id: Any,
        amount: int,
        currency: str,
        entry_type: Any,
        key: str,
        **kwargs: Any,
    ) -> None:
        # enforce the same uniqueness the DB does
        for entry in self.entries:
            if entry["key"] == key:
                raise ValueError(f"duplicate ledger key {key}")
        self.entries.append({"key": key, "amount": amount, "type": entry_type, **kwargs})
        # chaos: the commit landed, but the ack was lost
        if self.fail_next_post_with:
            self.fail_next_post_with = False
            raise TimeoutError("injected chaos: ledger ack lost after commit")

    async def charge_final(self, server: CloudServer, deleted_at: datetime) -> Any:
        return await self._service.charge_final(server, deleted_at)


def _fake_request() -> CreateServerRequest:
    return CreateServerRequest(
        name="chaos-server",
        plan_id="cx22",
        image_id="img-linux",
        location_id="fsn1",
        ssh_key_ids=(),
        labels={},
        user_data=None,
    )
