"""Monthly pool behavior through real checkout, selector, worker and Hetzner HTTP boundary.

The receipt store below is an application-port double, not PostgreSQL concurrency
proof. HTTP assertions observe actual mutations and settlement, not forwarded mocks.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from test_leaseweb_order_worker import FakeLedgerRepo, FakeRenewalRepo
from test_monthly_checkout import (
    FakeAccountRepo,
    FakeAuditRepo,
    FakeHoldRepo,
    FakeOfferRepo,
    FakeOperationRepo,
    FakeServerRepo,
    FakeWalletRepo,
    _offer,
    _user,
)

from cloud_platform.core.money import Money
from cloud_platform.modules.checkout.service import (
    CheckoutProviderUnavailableError,
    MonthlyCheckoutService,
    OfferUnavailableError,
    ProviderAccountCapacityError,
)
from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.operations.create_attempts import CreateAttemptConflict
from cloud_platform.modules.operations.domain import Operation, OperationStatus
from cloud_platform.modules.orders.domain import OrderStatus, ProviderOrder, SettlementStatus
from cloud_platform.modules.orders.service import OrderRecoveryService, OrderWorker
from cloud_platform.modules.provider_routes.domain import ProviderRoute, RouteState
from cloud_platform.modules.provider_routes.service import ProviderRouteSelector
from cloud_platform.modules.wallet.domain import HoldStateConflictError, HoldStatus, LedgerEntryType
from cloud_platform.providers.base import AccountServerUsage
from cloud_platform.providers.errors import ProviderCapacityError, ProviderUnavailable
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.registry import ProviderRegistry

ACCOUNTS = ("hz-main", "hz-next")


def _envelope(key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        key: items,
        "meta": {
            "pagination": {
                "page": 1,
                "per_page": 50,
                "previous_page": None,
                "next_page": None,
                "last_page": 1,
                "total_entries": len(items),
            }
        },
    }


class Servers(FakeServerRepo):
    async def create(self, server: Any, intent: Any) -> Any:
        server.idempotency_key = intent.idempotency_key
        return await super().create(server, intent)

    async def get(self, server_id: UUID) -> Any:
        return next((server for server in self.servers if server.id == server_id), None)

    async def list_requested_prepaid(self) -> list[Any]:
        return [s for s in self.servers if s.state is ServerLifecycleState.REQUESTED]


class Orders:
    def __init__(self) -> None:
        self.by_server: dict[UUID, ProviderOrder] = {}

    async def create(self, **facts: Any) -> ProviderOrder:
        order = ProviderOrder(id=uuid4(), **facts)
        self.by_server[order.server_id] = order
        return order

    async def get_by_server(self, server_id: UUID) -> ProviderOrder | None:
        return copy.deepcopy(self.by_server.get(server_id))

    async def get(self, order_id: UUID) -> ProviderOrder | None:
        return next((copy.deepcopy(o) for o in self.by_server.values() if o.id == order_id), None)

    async def save(self, order: ProviderOrder) -> ProviderOrder:
        self.by_server[order.server_id] = copy.deepcopy(order)
        return order

    async def list_outcome_unknown(self, provider_key: str, limit: int = 50) -> list[ProviderOrder]:
        return [
            copy.deepcopy(order)
            for order in self.by_server.values()
            if order.provider_key == provider_key and order.status is OrderStatus.OUTCOME_UNKNOWN
        ][:limit]


class Operations(FakeOperationRepo):
    async def get_by_key(self, key: str) -> Operation | None:
        return copy.deepcopy(self.ops.get(key))

    async def save(self, op: Operation) -> Operation:
        self.ops[op.operation_key] = copy.deepcopy(op)
        return copy.deepcopy(op)

    async def claim(self, operation_id: UUID) -> Operation | None:
        op = next(o for o in self.ops.values() if o.id == operation_id)
        if op.status is not OperationStatus.PENDING:
            return None
        claimed = copy.deepcopy(op)
        claimed.mark_in_flight()
        return await self.save(claimed)


class Usage:
    def __init__(self) -> None:
        self.counts: dict[str, int | Exception] = {"hz-main": 4, "hz-next": 2}
        self.reads: list[str] = []

    def accepts_new_orders(self, account_id: str) -> bool:
        return account_id in self.counts

    async def server_usage(self, account_id: str) -> AccountServerUsage:
        self.reads.append(account_id)
        count = self.counts[account_id]
        if isinstance(count, Exception):
            raise count
        return AccountServerUsage(account_id, count, 5)


class Routes:
    async def list_for_location(self, key: str, location: str) -> list[ProviderRoute]:
        return [
            ProviderRoute(
                key,
                account,
                location,
                RouteState.ELIGIBLE_AVAILABLE,
                priority=index,
                product_ids=("cx22",),
            )
            for index, account in enumerate(ACCOUNTS)
        ]

    async def account_ids_for_provider(self, key: str) -> tuple[str, ...]:
        return ACCOUNTS


class Receipts:
    """Independent durable state with generations; rejects unproven rebinds."""

    def __init__(self, flow: MonthlyFlow) -> None:
        self.flow = flow
        self.fail_refusal = False
        self.fence_before_sent = False
        self.fence_before_acceptance = False
        self.fence_after_refusal = False

    def _op(self, operation_id: UUID, generation: int | None = None) -> Operation:
        op = next(o for o in self.flow.ops.ops.values() if o.id == operation_id)
        if generation is not None and op.attempts != generation:
            raise CreateAttemptConflict("stale claim")
        return copy.deepcopy(op)

    async def claim(
        self,
        operation_id: UUID,
        server_id: UUID,
        order_id: UUID | None = None,
        catalog_account_id: str | None = None,
    ) -> Operation | None:
        op = self._op(operation_id)
        if op.status is not OperationStatus.PENDING:
            return None
        if "create_routing" not in (op.provider_response or {}):
            if op.attempts or self.flow.orders.by_server[server_id].post_attempted_at:
                raise CreateAttemptConflict("historical attempted create has no safe receipt")
            op.provider_response = {
                "create_routing": {
                    "version": 1,
                    "policy": "capacity_failover",
                    "catalog_account_id": catalog_account_id,
                    "attempts": [],
                }
            }
        op.mark_in_flight()
        return await self.flow.ops.save(op)

    async def start_attempt(
        self, operation_id: UUID, generation: int, account_id: str, *, request_facts: Any = None
    ) -> Operation:
        if self.fence_before_sent:
            self.flow.ops.ops[self._op(operation_id).operation_key].attempts += 1
        op = self._op(operation_id, generation)
        attempts = op.provider_response["create_routing"]["attempts"]
        if attempts and attempts[-1]["phase"] != "capacity_refused":
            raise CreateAttemptConflict("no proof authorizes a subsequent mutation")
        attempts.append({"sequence": len(attempts) + 1, "account_id": account_id, "phase": "sent"})
        server = await self.flow.servers.get(self.flow.result.server.id)
        order = self.flow.orders.by_server[server.id]
        server.credential_account_id = account_id
        order.credential_account_id = account_id
        order.post_attempted_at = datetime.now(UTC)
        return await self.flow.ops.save(op)

    async def record_refusal(
        self,
        operation_id: UUID,
        generation: int,
        account_id: str,
        *,
        capacity: bool,
        error_code: str | None = None,
        quota_names: tuple[str, ...] = (),
    ) -> Operation:
        if self.fail_refusal:
            raise RuntimeError("lost refusal commit acknowledgement")
        op = self._op(operation_id, generation)
        attempt = op.provider_response["create_routing"]["attempts"][-1]
        if attempt["account_id"] != account_id or attempt["phase"] != "sent":
            raise CreateAttemptConflict("refusal does not own the current mutation")
        attempt.update(phase="capacity_refused" if capacity else "refused")
        if error_code is not None:
            attempt["error_code"] = error_code
        if quota_names:
            attempt["quota_names"] = list(quota_names)
        saved = await self.flow.ops.save(op)
        if self.fence_after_refusal:
            self.flow.ops.ops[op.operation_key].attempts += 1
        return saved

    async def record_unknown(
        self, operation_id: UUID, generation: int, account_id: str
    ) -> Operation:
        op = self._op(operation_id, generation)
        attempt = op.provider_response["create_routing"]["attempts"][-1]
        if attempt["account_id"] != account_id:
            raise CreateAttemptConflict("unknown account mismatch")
        attempt["phase"] = "outcome_unknown"
        op.status = OperationStatus.OUTCOME_UNKNOWN
        self.flow.orders.by_server[self.flow.result.server.id].mark_outcome_unknown(
            "create outcome unknown"
        )
        return await self.flow.ops.save(op)

    async def record_acceptance(
        self,
        operation_id: UUID,
        generation: int,
        account_id: str,
        provider_server_id: str,
        **ips: Any,
    ) -> Operation:
        if self.fence_before_acceptance:
            self.flow.ops.ops[self._op(operation_id).operation_key].attempts += 1
        op = self._op(operation_id, generation)
        receipt = op.provider_response["create_routing"]
        attempt = receipt["attempts"][-1]
        if attempt["account_id"] != account_id or attempt["phase"] not in (
            "sent",
            "outcome_unknown",
        ):
            raise CreateAttemptConflict("acceptance does not own this mutation")
        attempt.update(phase="accepted", provider_server_id=provider_server_id)
        receipt["accepted_account_id"] = account_id
        op.provider_response["provider_server_id"] = provider_server_id
        server = self.flow.result.server
        server.provider_server_id = provider_server_id
        order = self.flow.orders.by_server[server.id]
        order.provider_order_id = provider_server_id
        order.status = OrderStatus.SUBMITTED
        return await self.flow.ops.save(op)

    async def save_outcome(
        self,
        operation_id: UUID,
        generation: int,
        status: OperationStatus,
        *,
        error: str | None = None,
        correlation: Any = None,
    ) -> Operation:
        op = self._op(operation_id, generation)
        op.status = status
        op.error = error
        op.provider_response.update(correlation or {})
        if status is OperationStatus.FAILED:
            server = self.flow.result.server
            server.state = ServerLifecycleState.ERROR
            self.flow.orders.by_server[server.id].mark_failed(error or "create failed")
        return await self.flow.ops.save(op)

    async def resume_safe_claim(self, operation_id: UUID, generation: int) -> Operation:
        op = self._op(operation_id, generation)
        attempts = op.provider_response["create_routing"]["attempts"]
        op.attempts += 1
        if not attempts or attempts[-1]["phase"] == "capacity_refused":
            op.status = OperationStatus.PENDING
        elif attempts[-1]["phase"] == "refused":
            op.status = OperationStatus.IN_FLIGHT
        else:
            op.status = OperationStatus.OUTCOME_UNKNOWN
        return await self.flow.ops.save(op)


class Settlement:
    def __init__(self, flow: MonthlyFlow) -> None:
        self.flow = flow
        self.captures = 0
        self.releases = 0

    async def capture_hold(self, wallet_id: UUID, hold_id: UUID, key: str) -> Any:
        hold = self.flow.holds.by_id[hold_id]
        # An accepted resource must be durable before any money mutation.
        assert self.flow.result.server.provider_server_id is not None
        assert self.flow.orders.by_server[self.flow.result.server.id].provider_order_id is not None
        if hold.status is HoldStatus.CREATED:
            self.captures += 1
            self.flow.wallet.wallet.balance -= hold.amount
            hold.capture()
        charge_key = f"capture-{key}"
        if await self.flow.ledger.get_entry_by_idempotency(wallet_id, charge_key) is None:
            await self.flow.ledger.post_entry(
                wallet_id, hold.amount, hold.currency, LedgerEntryType.CHARGE, charge_key
            )
        return hold

    async def release_hold(self, wallet_id: UUID, hold_id: UUID, key: str) -> Any:
        hold = self.flow.holds.by_id[hold_id]
        if hold.status is HoldStatus.CREATED:
            self.releases += 1
            hold.release()
        return hold


class MonthlyFlow:
    def __init__(self) -> None:
        self.offer = replace(
            _offer(),
            provider_key="hetzner",
            product_id="cx22",
            location_id="fsn1",
            provider_account_id="hz-main",
            provider_cost_minor=999,
        )
        self.offers = FakeOfferRepo(self.offer)
        self.wallet = FakeWalletRepo(10_000)
        self.holds = FakeHoldRepo(self.wallet)
        self.servers = Servers()
        self.orders = Orders()
        self.ops = Operations()
        self.audit = FakeAuditRepo()
        self.ledger = FakeLedgerRepo()
        self.usage = Usage()
        self.selector = ProviderRouteSelector(
            repository=Routes(), usage_readers={"hetzner": self.usage}
        )
        self.receipts = Receipts(self)
        self.settlement = Settlement(self)
        self.registry = ProviderRegistry()
        self.registry.disable_default_account_fallback("hetzner")
        self.clients: list[HetznerCloudProvider] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[tuple[str, str, dict[str, str]]] = []
        self.behavior = {account: "accepted" for account in ACCOUNTS}
        self.prices = {account: "9.99" for account in ACCOUNTS}
        self.currencies = {account: "EUR" for account in ACCOUNTS}
        self.remote: dict[str, dict[str, Any]] = {}
        self.recover_match = False
        self.recovery_mode = "matched"
        self.result: Any = None

    async def open(self) -> None:
        for account in ACCOUNTS:
            provider = HetznerCloudProvider("placeholder", account_id=account)
            await provider._client.aclose()
            provider._client = httpx.AsyncClient(
                base_url="https://hetzner.invalid/v1",
                transport=httpx.MockTransport(
                    lambda request, account=account: self.http(account, request)
                ),
            )
            self.clients.append(provider)
            self.registry.register_route("hetzner", account, provider)
        self.checkout = MonthlyCheckoutService(
            server_repo=self.servers,
            offers_repo=self.offers,
            account_repo=FakeAccountRepo(),
            wallet_repo=self.wallet,
            hold_repo=self.holds,
            orders_repo=self.orders,
            operation_repo=self.ops,
            audit_repo=self.audit,
            provider_registry=self.registry,
            fulfillment_routes=self.selector,
        )
        self.worker = OrderWorker(
            server_repo=self.servers,
            offers_repo=self.offers,
            orders_repo=self.orders,
            operation_repo=self.ops,
            wallet_repo=self.wallet,
            hold_repo=self.holds,
            hold_service=self.settlement,
            ledger_repo=self.ledger,
            audit_repo=self.audit,
            provider_registry=self.registry,
            renewal_repo=FakeRenewalRepo(),
            fulfillment_routes=self.selector,
            create_attempts=self.receipts,
        )

    def recovery_service(self) -> OrderRecoveryService:
        return OrderRecoveryService(
            server_repo=self.servers,
            offers_repo=FakeOfferRepo(self.offer),
            orders_repo=self.orders,
            operation_repo=self.ops,
            wallet_repo=self.wallet,
            hold_repo=self.holds,
            hold_service=self.settlement,
            audit_repo=self.audit,
            provider_registry=self.registry,
            create_attempts=self.receipts,
            settlement=self.worker._settlement,
        )

    def http(self, account: str, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        if request.method == "GET":
            self.reads.append((account, path, dict(request.url.params)))
            if path == "/pricing":
                return httpx.Response(200, json={"pricing": {"currency": self.currencies[account]}})
            if path == "/server_types":
                if self.behavior[account] == "preflight_auth":
                    return httpx.Response(
                        403, json={"error": {"code": "forbidden", "message": "denied"}}
                    )
                return httpx.Response(
                    200,
                    json=_envelope(
                        "server_types",
                        [
                            {
                                "id": 22,
                                "name": "cx22",
                                "cores": 2,
                                "memory": 4,
                                "disk": 40,
                                "architecture": "x86",
                                "locations": [{"name": "fsn1", "available": True}],
                                "prices": [
                                    {
                                        "location": "fsn1",
                                        "price_monthly": {"gross": self.prices[account]},
                                        "price_hourly": {"gross": "0.015"},
                                    }
                                ],
                            }
                        ],
                    ),
                )
            if path == "/images":
                return httpx.Response(
                    200,
                    json=_envelope(
                        "images",
                        [
                            {
                                "id": 1,
                                "name": "ubuntu-24.04",
                                "type": "system",
                                "status": "available",
                                "architecture": "x86",
                                "os_flavor": "ubuntu",
                                "deprecated": None,
                            }
                        ],
                    ),
                )
            if path == "/servers":
                servers = (
                    [self.remote[account]] if self.recover_match and account in self.remote else []
                )
                if self.recover_match and self.recovery_mode == "conflicting" and servers:
                    servers = [copy.deepcopy(servers[0])]
                    servers[0]["labels"]["platform_server_id"] = str(uuid4())
                if self.recover_match and self.recovery_mode == "ambiguous" and servers:
                    servers = [servers[0], {**servers[0], "id": 303}]
                if self.recover_match and self.recovery_mode == "scan_failed":
                    return httpx.Response(200, json={"servers": []})
                return httpx.Response(
                    200,
                    json={
                        "servers": servers,
                        "meta": {
                            "pagination": {
                                "page": 1,
                                "per_page": 50,
                                "previous_page": None,
                                "next_page": None,
                                "last_page": 1,
                                "total_entries": len(servers),
                            }
                        },
                    },
                )
            if path.startswith("/servers/") and account in self.remote:
                return httpx.Response(200, json={"server": self.remote[account]})
            raise AssertionError(f"unexpected read: {account} {path}")
        assert request.method == "POST" and path == "/servers"
        body = json.loads(request.content)
        # These are observations at the real mutation boundary, not receipt-double echoes.
        op = next(iter(self.ops.ops.values()))
        attempt = op.provider_response["create_routing"]["attempts"][-1]
        assert attempt["phase"] == "sent" and attempt["account_id"] == account
        assert self.result.server.credential_account_id == account
        assert self.orders.by_server[self.result.server.id].credential_account_id == account
        assert self.orders.by_server[self.result.server.id].post_attempted_at is not None
        assert self.result.hold.status is HoldStatus.CREATED
        if account == "hz-next" and any(a == "hz-main" for a, _ in self.posts):
            assert (
                op.provider_response["create_routing"]["attempts"][0]["phase"] == "capacity_refused"
            )
        self.posts.append((account, body))
        remote = {
            "id": 101 if account == "hz-main" else 202,
            "name": body["name"],
            "status": "initializing",
            "labels": body["labels"],
            "server_type": {"name": body["server_type"]},
            "image": {"name": body["image"]},
            "datacenter": {"location": {"name": body["location"]}},
            "public_net": {"ipv4": {"ip": "192.0.2.2"}},
        }
        self.remote[account] = remote
        behavior = self.behavior[account]
        if behavior == "quota":
            del self.remote[account]
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": "resource_limit_exceeded",
                        "message": "quota",
                        "details": {"limits": [{"name": "project_limit"}]},
                    }
                },
            )
        if behavior == "service_error":
            return httpx.Response(
                422, json={"error": {"code": "service_error", "message": "Error within a service"}}
            )
        if behavior in ("auth", "invalid"):
            del self.remote[account]
            return httpx.Response(
                403 if behavior == "auth" else 422,
                json={
                    "error": {
                        "code": "forbidden" if behavior == "auth" else "invalid_input",
                        "message": "rejected",
                    }
                },
            )
        if behavior == "timeout":
            raise httpx.ReadTimeout("response lost", request=request)
        if behavior == "5xx":
            return httpx.Response(
                503, json={"error": {"code": "service_error", "message": "unknown"}}
            )
        if behavior == "malformed":
            return httpx.Response(201, content=b"not-json")
        if behavior == "missing_id":
            return httpx.Response(201, json={"server": {"name": body["name"]}})
        if behavior == "mismatched":
            return httpx.Response(201, json={"server": {**remote, "name": "foreign-server"}})
        return httpx.Response(201, json={"server": remote})

    async def buy(self, key: str = "bot-monthly:opaque") -> Any:
        self.result = await self.checkout.create_order(
            user=_user(),
            offer_id=self.offer.id,
            os_name="ubuntu-24.04",
            idempotency_key=key,
            expected_selling_price_minor=1299,
            expected_selling_currency="EUR",
        )
        return self.result

    @property
    def op(self) -> Operation:
        return next(iter(self.ops.ops.values()))

    @property
    def phases(self) -> list[str]:
        return [a["phase"] for a in self.op.provider_response["create_routing"]["attempts"]]


@pytest_asyncio.fixture
async def flow() -> Any:
    value = MonthlyFlow()
    await value.open()
    try:
        yield value
    finally:
        for client in value.clients:
            await client._client.aclose()


def assert_settled_once(flow: MonthlyFlow, account: str, provider_id: str) -> None:
    server = flow.result.server
    order = flow.orders.by_server[server.id]
    assert server.credential_account_id == order.credential_account_id == account
    assert server.provider_server_id == order.provider_order_id == provider_id
    assert flow.op.status is OperationStatus.COMPLETED
    assert order.settlement_status is SettlementStatus.COMPLETE
    assert flow.result.hold.status is HoldStatus.CAPTURED
    assert flow.settlement.captures == 1 and flow.settlement.releases == 0
    assert flow.wallet.wallet.balance == 10_000 - 1299
    assert len(flow.ledger.entries) == 1
    assert flow.ledger.post_count == 1
    charge = next(iter(flow.ledger.entries.values()))
    assert charge.entry_type is LedgerEntryType.CHARGE
    assert charge.amount == Money("1299", "EUR")
    assert len(flow.servers.servers) == len(flow.orders.by_server) == len(flow.ops.ops) == 1
    assert flow.offer.provider_key == "hetzner" and flow.offer.provider_account_id == "hz-main"


async def test_full_preferred_account_skips_post_and_keeps_one_offer_provenance(
    flow: MonthlyFlow,
) -> None:
    flow.usage.counts["hz-main"] = 5
    await flow.buy()
    assert flow.result.server.credential_account_id == "hz-next"
    assert flow.offer.provider_account_id == "hz-main"
    assert flow.posts == []
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-next"]
    assert flow.phases == ["accepted"]
    assert_settled_once(flow, "hz-next", "202")


async def test_documented_403_continues_same_intent_without_changing_contract_or_money(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    original_ids = (flow.result.server.id, flow.result.order.id, flow.op.id, flow.result.hold.id)
    original_contract = copy.deepcopy(flow.orders.by_server[flow.result.server.id])
    flow.behavior["hz-main"] = "quota"
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == list(ACCOUNTS)
    first, second = [body for _, body in flow.posts]
    assert first == second
    assert second["server_type"] == "cx22" and second["location"] == "fsn1"
    assert second["image"] == "ubuntu-24.04"
    assert second["labels"]["provider_price_minor"] == "999"
    assert second["labels"]["provider_currency"] == "EUR"
    receipt = flow.op.provider_response["create_routing"]
    assert flow.phases == ["capacity_refused", "accepted"]
    assert receipt["attempts"][0]["error_code"] == "resource_limit_exceeded"
    assert receipt["attempts"][0]["quota_names"] == ["project_limit"]
    assert receipt["accepted_account_id"] == "hz-next"
    order = flow.orders.by_server[flow.result.server.id]
    for field in (
        "product_id",
        "location_id",
        "os_name",
        "contract_term",
        "billing_cycle",
        "provider_cost_minor",
        "provider_cost_currency",
        "selling_price_minor",
        "selling_currency",
    ):
        assert getattr(order, field) == getattr(original_contract, field)
    assert (flow.result.server.id, order.id, flow.op.id, flow.result.hold.id) == original_ids
    assert_settled_once(flow, "hz-next", "202")


@pytest.mark.parametrize("failure", ["timeout", "5xx", "malformed", "missing_id", "mismatched"])
async def test_unknown_create_never_changes_account_or_releases_reservation(
    flow: MonthlyFlow, failure: str
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = failure
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-main"]
    assert flow.phases == ["outcome_unknown"]
    assert flow.op.status is OperationStatus.OUTCOME_UNKNOWN
    assert flow.orders.by_server[flow.result.server.id].status is OrderStatus.OUTCOME_UNKNOWN
    assert flow.result.server.credential_account_id == "hz-main"
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0
    assert flow.wallet.wallet.balance == 10_000 and not flow.ledger.entries
    scans = [(a, params) for a, path, params in flow.reads if path == "/servers"]
    assert scans and all(a == "hz-main" for a, _ in scans)
    assert all("label_selector" not in params for _, params in scans)


async def test_exact_same_account_recovery_attaches_identity_and_settles_once(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "timeout"
    flow.recover_match = True
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-main"]
    assert flow.phases == ["accepted"]
    assert all(a == "hz-main" for a, path, _ in flow.reads if path.startswith("/servers"))
    assert_settled_once(flow, "hz-main", "101")


@pytest.mark.parametrize("failure", ["auth", "invalid"])
async def test_noncapacity_mutation_refusal_does_not_switch(
    flow: MonthlyFlow, failure: str
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = failure
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-main"]
    assert flow.phases == ["refused"]
    assert flow.result.server.provider_server_id is None
    assert flow.settlement.captures == 0 and flow.settlement.releases == 1
    assert flow.result.hold.status is HoldStatus.RELEASED
    assert flow.wallet.wallet.balance == 10_000 and not flow.ledger.entries


@pytest.mark.parametrize("error_code", [None, "resource_limit_exceeded", "some_other_limit"])
async def test_unproven_capacity_code_is_not_account_continuation_authority(
    flow: MonthlyFlow,
    error_code: str | None,
) -> None:
    await flow.buy()

    async def reject(request: Any, key: Any) -> Any:
        raise ProviderCapacityError("not authoritative server quota proof", error_code=error_code)

    flow.clients[0].create_server = reject
    await flow.worker.process_pending()
    assert flow.posts == []
    assert flow.phases == ["outcome_unknown"]
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0


@pytest.mark.parametrize("preflight", ["auth", "price"])
async def test_readonly_auth_or_price_mismatch_never_creates_capacity_receipt(
    flow: MonthlyFlow, preflight: str
) -> None:
    await flow.buy()
    if preflight == "auth":
        flow.behavior["hz-main"] = "preflight_auth"
    else:
        flow.prices = {a: "12.99" for a in ACCOUNTS}
    await flow.worker.process_pending()
    assert not flow.posts
    assert flow.phases == []
    assert flow.op.status is OperationStatus.FAILED
    assert flow.settlement.releases == 1 and flow.settlement.captures == 0
    assert not flow.ledger.entries


async def test_genuine_all_full_blocks_checkout_before_money_or_intents(flow: MonthlyFlow) -> None:
    flow.usage.counts = {a: 5 for a in ACCOUNTS}
    with pytest.raises(ProviderAccountCapacityError):
        await flow.buy()
    assert not flow.servers.servers and not flow.orders.by_server and not flow.ops.ops
    assert not flow.holds.holds and not flow.posts and not flow.ledger.entries


async def test_unreadable_capacity_is_unavailable_not_all_full_and_creates_no_intent(
    flow: MonthlyFlow,
) -> None:
    flow.usage.counts = {"hz-main": 5, "hz-next": ProviderUnavailable("inventory unreadable")}
    with pytest.raises(CheckoutProviderUnavailableError):
        await flow.buy()
    assert not flow.servers.servers and not flow.orders.by_server and not flow.ops.ops
    assert not flow.holds.holds and not flow.posts


async def test_worker_rechecks_capacity_before_first_post(flow: MonthlyFlow) -> None:
    await flow.buy()
    flow.usage.counts["hz-main"] = 5
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-next"]
    assert_settled_once(flow, "hz-next", "202")


async def test_refusal_commit_failure_forbids_next_post_and_retains_hold(flow: MonthlyFlow) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "quota"
    flow.receipts.fail_refusal = True
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-main"]
    assert flow.phases == ["sent"]
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0


@pytest.mark.parametrize("fence", ["before_sent", "before_acceptance"])
async def test_stale_claim_cannot_post_or_settle_or_move_to_another_account(
    flow: MonthlyFlow, fence: str
) -> None:
    await flow.buy()
    setattr(flow.receipts, f"fence_{fence}", True)
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ([] if fence == "before_sent" else ["hz-main"])
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0
    assert not flow.ledger.entries


async def test_stale_sent_receipt_never_reposts_even_if_preferred_account_now_full(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    claimed = await flow.receipts.claim(flow.op.id, flow.result.server.id, flow.result.order.id)
    await flow.receipts.start_attempt(claimed.id, claimed.attempts, "hz-main")
    flow.op.updated_at = datetime.now(UTC) - timedelta(hours=1)
    flow.usage.counts["hz-main"] = 5
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert not flow.posts
    assert flow.result.server.credential_account_id == "hz-main"
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0


@pytest.mark.parametrize("historical_status", [OperationStatus.IN_FLIGHT, OperationStatus.PENDING])
async def test_historical_attempt_without_receipt_never_gains_pool_routing(
    flow: MonthlyFlow,
    historical_status: OperationStatus,
) -> None:
    await flow.buy()
    op = flow.op
    op.mark_in_flight()
    if historical_status is OperationStatus.PENDING:
        op.requeue("historical retry lacks pre-POST proof")
    op.updated_at = datetime.now(UTC) - timedelta(hours=1)
    flow.orders.by_server[flow.result.server.id].post_attempted_at = op.updated_at
    flow.usage.counts["hz-main"] = 0
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert not flow.posts
    assert "create_routing" not in (flow.op.provider_response or {})
    assert flow.op.status is OperationStatus.OUTCOME_UNKNOWN
    assert flow.result.server.credential_account_id == "hz-main"
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0


async def test_confirmed_selling_price_change_blocks_before_hold(flow: MonthlyFlow) -> None:
    flow.offer = replace(flow.offer, selling_price_minor=1499)
    flow.offers._offer = flow.offer
    with pytest.raises(OfferUnavailableError):
        await flow.buy()
    assert not flow.posts and not flow.holds.holds and not flow.servers.servers


async def test_replay_uses_original_contract_even_after_catalog_and_capacity_change(
    flow: MonthlyFlow,
) -> None:
    original = await flow.buy()
    flow.offer = replace(flow.offer, selling_price_minor=1499)
    flow.offers._offer = flow.offer
    flow.usage.counts = {a: 5 for a in ACCOUNTS}
    replay = await flow.buy()
    assert replay.replayed and replay.server.id == original.server.id
    assert replay.order.id == original.order.id and replay.hold.id == original.hold.id
    assert replay.order.selling_price_minor == 1299 and replay.order.os_name == "ubuntu-24.04"
    assert len(flow.holds.holds) == len(flow.servers.servers) == 1
    assert not flow.posts


async def test_unregistered_provider_keeps_priority_routing_without_inventory_reads(
    flow: MonthlyFlow,
) -> None:
    assert not flow.selector.supports_capacity_failover("leaseweb")
    assert await flow.selector.account_for("leaseweb", "fsn1", "cx22") == "hz-main"
    assert not flow.usage.reads


async def test_all_accounts_refuse_releases_original_hold_once_without_charge(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.behavior = {account: "quota" for account in ACCOUNTS}
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == list(ACCOUNTS)
    assert flow.phases == ["capacity_refused", "capacity_refused"]
    assert flow.op.status is OperationStatus.FAILED
    assert flow.result.hold.status is HoldStatus.RELEASED
    assert flow.settlement.releases == 1 and flow.settlement.captures == 0
    assert not flow.ledger.entries and flow.wallet.wallet.balance == 10_000
    assert len(flow.servers.servers) == len(flow.orders.by_server) == len(flow.holds.holds) == 1


async def test_generation_lost_after_refusal_forbids_second_mutation(flow: MonthlyFlow) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "quota"
    flow.receipts.fence_after_refusal = True
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-main"]
    assert flow.phases == ["capacity_refused"]
    assert flow.result.server.credential_account_id == "hz-main"
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0


async def test_alternative_must_supply_original_native_price_not_repriced_catalog(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "quota"
    flow.prices["hz-next"] = "12.99"
    flow.offer = replace(flow.offer, provider_cost_minor=1299, selling_price_minor=1599)
    flow.offers._offer = flow.offer
    await flow.worker.process_pending()
    assert [a for a, _ in flow.posts] == ["hz-main"]
    assert flow.phases == ["capacity_refused"]
    order = flow.orders.by_server[flow.result.server.id]
    assert order.provider_cost_minor == 999 and order.selling_price_minor == 1299
    assert flow.result.hold.amount == 1299 and flow.result.hold.status is HoldStatus.RELEASED
    assert flow.settlement.releases == 1 and not flow.ledger.entries


async def test_unreadable_worker_capacity_requeues_before_sent_without_releasing_hold(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.usage.counts = {a: ProviderUnavailable("inventory unavailable") for a in ACCOUNTS}
    await flow.worker.process_pending()
    assert not flow.posts and flow.phases == []
    assert flow.op.status is OperationStatus.PENDING
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0


async def test_delayed_recovery_attaches_only_original_account_after_no_match(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "timeout"
    await flow.worker.process_pending()
    assert flow.phases == ["outcome_unknown"]
    recovery = flow.recovery_service()
    await recovery.recover("hetzner")
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0
    flow.usage.counts["hz-main"] = 5
    flow.recover_match = True
    await recovery.recover("hetzner")
    await recovery.recover("hetzner")
    await flow.worker.process_pending()
    assert [account for account, _ in flow.posts] == ["hz-main"]
    assert all(
        account == "hz-main" for account, path, _ in flow.reads if path.startswith("/servers")
    )
    assert flow.phases == ["accepted"]
    assert_settled_once(flow, "hz-main", "101")


@pytest.mark.parametrize("recovery_mode", ["conflicting", "ambiguous", "scan_failed"])
async def test_unproven_delayed_recovery_never_attaches_or_releases_or_posts(
    flow: MonthlyFlow,
    recovery_mode: str,
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "timeout"
    await flow.worker.process_pending()
    flow.recover_match = True
    flow.recovery_mode = recovery_mode
    recovery = flow.recovery_service()
    await recovery.recover("hetzner")
    await recovery.recover("hetzner")
    assert [account for account, _ in flow.posts] == ["hz-main"]
    assert flow.phases == ["outcome_unknown"]
    assert flow.op.status is OperationStatus.OUTCOME_UNKNOWN
    assert flow.result.server.provider_server_id is None
    assert flow.result.server.credential_account_id == "hz-main"
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0
    assert not flow.ledger.entries


async def test_catalog_refresh_cannot_replace_original_monthly_credential_provenance(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.offer = replace(flow.offer, provider_account_id="hz-next")
    flow.offers._offer = flow.offer
    flow.behavior["hz-main"] = "quota"
    await flow.worker.process_pending()
    assert flow.result.server.credential_account_id == "hz-next"
    assert flow.result.server.offer_fingerprint["provider_account_id"] == "hz-main"
    assert flow.op.provider_response["create_routing"]["catalog_account_id"] == "hz-main"
    assert flow.result.order.provider_cost_minor == 999
    assert flow.result.order.selling_price_minor == 1299
    assert flow.settlement.captures == 1


async def test_unqualified_catalog_account_blocks_monthly_intent_before_funds(
    flow: MonthlyFlow,
) -> None:
    flow.offer = replace(flow.offer, provider_account_id=None)
    flow.offers._offer = flow.offer
    with pytest.raises(OfferUnavailableError):
        await flow.buy()
    assert not flow.holds.holds and not flow.servers.servers and not flow.posts


async def test_alternative_project_currency_must_match_original_native_contract(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "quota"
    flow.currencies["hz-next"] = "USD"
    await flow.worker.process_pending()
    assert [account for account, _ in flow.posts] == ["hz-main"]
    assert flow.result.server.provider_server_id is None
    assert flow.result.server.state is ServerLifecycleState.ERROR
    assert flow.result.hold.status is HoldStatus.RELEASED
    assert flow.settlement.captures == 0
    assert flow.result.order.provider_cost_currency == "EUR"
    assert flow.result.order.provider_cost_minor == 999


@pytest.mark.parametrize("delayed_recovery", [False, True])
@pytest.mark.parametrize("settlement_failure", ["transient", "released_hold"])
async def test_accepted_monthly_server_stays_requested_until_charge_is_settled(
    flow: MonthlyFlow, monkeypatch, delayed_recovery: bool, settlement_failure: str
) -> None:
    await flow.buy()
    if settlement_failure == "released_hold":

        async def released_capture(*args):
            flow.result.hold.release()
            raise HoldStateConflictError("reservation released during settlement")

        monkeypatch.setattr(flow.settlement, "capture_hold", released_capture)
    else:

        async def unavailable_capture(*args):
            raise RuntimeError("local capture unavailable")

        monkeypatch.setattr(flow.settlement, "capture_hold", unavailable_capture)
    if delayed_recovery:
        flow.behavior["hz-main"] = "timeout"
    await flow.worker.process_pending()
    if delayed_recovery:
        flow.recover_match = True
        await flow.recovery_service().recover("hetzner")
    assert [account for account, _ in flow.posts] == ["hz-main"]
    assert flow.op.status is OperationStatus.COMPLETED
    assert flow.result.server.provider_server_id == "101"
    assert flow.result.server.state is ServerLifecycleState.REQUESTED
    assert flow.phases == ["accepted"]
    assert flow.settlement.captures == flow.settlement.releases == 0
    assert not flow.ledger.entries
    expected_order_status = (
        OrderStatus.NEEDS_REVIEW if settlement_failure == "released_hold" else OrderStatus.SUBMITTED
    )
    assert flow.orders.by_server[flow.result.server.id].status is expected_order_status


async def test_terminal_refusal_crash_is_fenced_and_releases_without_a_second_post(
    flow: MonthlyFlow, monkeypatch
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "invalid"
    save_outcome = flow.receipts.save_outcome

    async def crash_after_refusal(*args, **kwargs):
        if args[2] is OperationStatus.FAILED:
            raise RuntimeError("crash after durable refusal")
        return await save_outcome(*args, **kwargs)

    monkeypatch.setattr(flow.receipts, "save_outcome", crash_after_refusal)
    await flow.worker.process_pending()
    old_generation = flow.op.attempts
    assert flow.phases == ["refused"]
    assert flow.result.hold.status is HoldStatus.CREATED
    monkeypatch.setattr(flow.receipts, "save_outcome", save_outcome)
    flow.op.updated_at = datetime.now(UTC) - timedelta(hours=1)
    await flow.worker.process_pending()
    await flow.worker.process_pending()
    assert [account for account, _ in flow.posts] == ["hz-main"]
    assert flow.op.attempts > old_generation
    assert flow.op.status is OperationStatus.FAILED
    assert flow.result.server.state is ServerLifecycleState.ERROR
    assert flow.result.server.credential_account_id == "hz-main"
    assert flow.result.hold.status is HoldStatus.RELEASED
    assert flow.settlement.releases == 1 and flow.settlement.captures == 0


@pytest.mark.parametrize("historical_status", [OperationStatus.IN_FLIGHT, OperationStatus.PENDING])
@pytest.mark.parametrize("recovery_mode", ["matched", "conflicting"])
async def test_historical_monthly_attempt_recovers_only_the_original_direct_account(
    flow: MonthlyFlow, historical_status: OperationStatus, recovery_mode: str
) -> None:
    await flow.buy()
    flow.op.mark_in_flight()
    if historical_status is OperationStatus.PENDING:
        flow.op.requeue("pre-cutover retry")
    flow.op.updated_at = datetime.now(UTC) - timedelta(hours=1)
    flow.orders.by_server[flow.result.server.id].post_attempted_at = flow.op.updated_at
    flow.remote["hz-main"] = {
        "id": 101,
        "name": f"srv-{flow.result.server.id.hex[:8]}",
        "status": "running",
        "server_type": {"name": "cx22"},
        "image": {"name": "ubuntu-24.04"},
        "datacenter": {"location": {"name": "fsn1"}},
        "labels": {
            "platform-operation": flow.op.operation_key[:63],
            "platform_server_id": str(flow.result.server.id),
        },
        "public_net": {"ipv4": {"ip": "192.0.2.2"}},
    }
    await flow.worker.process_pending()
    flow.usage.counts["hz-main"] = 5
    flow.recover_match = True
    flow.recovery_mode = recovery_mode
    await flow.recovery_service().recover("hetzner")
    await flow.recovery_service().recover("hetzner")
    assert not flow.posts
    assert "create_routing" not in (flow.op.provider_response or {})
    assert flow.result.server.credential_account_id == "hz-main"
    if recovery_mode == "matched":
        assert flow.result.server.provider_server_id == "101"
        assert flow.result.server.state is ServerLifecycleState.PROVISIONING
        assert flow.result.hold.status is HoldStatus.CAPTURED
        assert flow.settlement.captures == 1 and flow.settlement.releases == 0
    else:
        assert flow.result.server.provider_server_id is None
        assert flow.op.status is OperationStatus.OUTCOME_UNKNOWN
        assert flow.result.hold.status is HoldStatus.CREATED
        assert flow.settlement.captures == flow.settlement.releases == 0


async def test_service_side_422_is_unknown_and_recovers_without_releasing_or_reposting(
    flow: MonthlyFlow,
) -> None:
    await flow.buy()
    flow.behavior["hz-main"] = "service_error"
    await flow.worker.process_pending()
    assert flow.op.status is OperationStatus.OUTCOME_UNKNOWN
    assert flow.phases == ["outcome_unknown"]
    assert flow.result.hold.status is HoldStatus.CREATED
    assert flow.settlement.captures == flow.settlement.releases == 0
    assert [account for account, _ in flow.posts] == ["hz-main"]
    flow.recover_match = True
    await flow.recovery_service().recover("hetzner")
    assert flow.result.server.provider_server_id == "101"
    assert flow.result.server.credential_account_id == "hz-main"
    assert flow.result.hold.status is HoldStatus.CAPTURED
    assert flow.settlement.captures == 1 and flow.settlement.releases == 0
    assert [account for account, _ in flow.posts] == ["hz-main"]
