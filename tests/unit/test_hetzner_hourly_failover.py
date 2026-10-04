"""Actual hourly checkout/worker routing with isolated ports and Hetzner HTTP envelopes.

The receipt port below models durable transitions, not PostgreSQL locking; real
claim/rebind atomicity is covered separately by the scratch-database tests.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.hourly.service import (
    HourlyAccountCapacityError,
    HourlyCloudService,
    HourlyNotAvailableError,
    HourlyProviderUnavailableError,
    _validate_hourly_contract,
)
from cloud_platform.modules.offers.domain import PricingPolicy
from cloud_platform.modules.offers.pricing import CatalogOfferPricer
from cloud_platform.modules.operations.create_attempts import CreateAttemptConflict
from cloud_platform.modules.operations.domain import OperationStatus
from cloud_platform.modules.provider_routes.domain import ProviderRoute, RouteState
from cloud_platform.modules.provider_routes.service import ProviderRouteSelector
from cloud_platform.providers.base import AccountServerUsage
from cloud_platform.providers.errors import ProviderCapacityError, ProviderError
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.hetzner.hourly import HetznerHourlyCloudProvider
from tests.unit.hourly_money import hourly_money
from tests.unit.test_hetzner_hourly_adapter import envelope, image, reply, server, server_type
from tests.unit.test_hourly_cloud_flow import (
    USER,
    FakeAccountRepo,
    FakeAuditRepo,
    FakeOffersRepo,
    FakeOpsRepo,
    FakeServerRepo,
    FakeSnapshots,
    FakeWalletRepo2,
    _offer,
    _UsdRates,
)


class ReceiptPort:
    """Fenced behavioral port: a failed refusal write cannot authorize another SENT."""

    def __init__(self, ops, servers, events):
        self.ops, self.servers, self.events = ops, servers, events
        self.fail_refusal = False

    def operation(self, operation_id, generation=None):
        op = next(op for op in self.ops.ops.values() if op.id == operation_id)
        if generation is not None and op.attempts != generation:
            raise CreateAttemptConflict("stale claim")
        return op

    async def claim(self, operation_id, server_id, order_id=None, catalog_account_id=None):
        op = self.operation(operation_id)
        if op.status is not OperationStatus.PENDING:
            return None
        op.mark_in_flight()
        op.provider_response = op.provider_response or {}
        op.provider_response.setdefault(
            "create_routing",
            {
                "version": 1,
                "policy": "capacity_failover",
                "catalog_account_id": catalog_account_id,
                "attempts": [],
            },
        )
        return op

    async def start_attempt(self, operation_id, generation, account_id, *, request_facts=None):
        op = self.operation(operation_id, generation)
        attempts = op.provider_response["create_routing"]["attempts"]
        if op.status is not OperationStatus.IN_FLIGHT or (
            attempts and attempts[-1]["phase"] != "capacity_refused"
        ):
            raise CreateAttemptConflict("no proof permitting POST")
        srv = self.servers.servers[op.resource_id]
        if srv.provider_server_id:
            raise CreateAttemptConflict("accepted resource")
        srv.credential_account_id = account_id
        attempts.append({"sequence": len(attempts) + 1, "account_id": account_id, "phase": "sent"})
        self.events.append((account_id, "sent"))
        return op

    def attempt(self, operation_id, generation, account_id):
        op = self.operation(operation_id, generation)
        attempt = op.provider_response["create_routing"]["attempts"][-1]
        if attempt["account_id"] != account_id:
            raise CreateAttemptConflict("wrong account")
        return op, attempt

    async def record_refusal(
        self, operation_id, generation, account_id, *, capacity, error_code=None, quota_names=()
    ):
        if self.fail_refusal:
            raise CreateAttemptConflict("refusal commit unavailable")
        op, attempt = self.attempt(operation_id, generation, account_id)
        attempt.update(
            phase="capacity_refused" if capacity else "refused",
            error_code=error_code,
            quota_names=list(quota_names),
        )
        self.events.append((account_id, attempt["phase"]))
        return op

    async def record_unknown(self, operation_id, generation, account_id):
        op, attempt = self.attempt(operation_id, generation, account_id)
        attempt["phase"] = "outcome_unknown"
        return op

    async def record_acceptance(
        self, operation_id, generation, account_id, provider_server_id, *, ipv4=None, ipv6=None
    ):
        op, attempt = self.attempt(operation_id, generation, account_id)
        attempt.update(phase="accepted", provider_server_id=provider_server_id)
        op.provider_response["create_routing"]["accepted_account_id"] = account_id
        op.provider_response["provider_server_id"] = provider_server_id
        srv = self.servers.servers[op.resource_id]
        srv.provider_server_id = provider_server_id
        srv.credential_account_id = account_id
        self.events.append((account_id, "accepted"))
        return op

    async def save_outcome(self, operation_id, generation, status, *, error=None, correlation=None):
        op = self.operation(operation_id, generation)
        op.status = status
        if correlation:
            op.provider_response.update(correlation)
        srv = self.servers.servers[op.resource_id]
        if status is OperationStatus.FAILED:
            srv.state = ServerLifecycleState.ERROR
        elif status is OperationStatus.COMPLETED:
            srv.state = ServerLifecycleState.PROVISIONING
        return op

    async def resume_safe_claim(self, operation_id, generation):
        op = self.operation(operation_id, generation)
        attempts = op.provider_response["create_routing"]["attempts"]
        op.attempts += 1
        if not attempts or attempts[-1]["phase"] == "capacity_refused":
            op.status = OperationStatus.PENDING
        elif attempts[-1]["phase"] == "refused":
            op.status = OperationStatus.IN_FLIGHT
        elif attempts[-1]["phase"] in {"sent", "outcome_unknown"}:
            attempts[-1]["phase"] = "outcome_unknown"
            op.status = OperationStatus.OUTCOME_UNKNOWN
        elif attempts[-1]["phase"] != "accepted":
            raise CreateAttemptConflict("no recovery proof")
        return op


class Pool:
    def __init__(self):
        self.counts = {"hz-a": 4, "hz-b": 2}
        self.failure = None
        self.b_image = "100"
        self.b_rate = "0.0075"
        self.events = []
        self.posts = []
        self.reads = []
        self.providers = {}
        self.adapters = {}
        self.original_clients = []
        self.ops, self.servers, self.snapshots = FakeOpsRepo(), FakeServerRepo(), FakeSnapshots()
        self.receipts = ReceiptPort(self.ops, self.servers, self.events)
        for account in self.counts:
            provider = HetznerCloudProvider("synthetic-token", account_id=account)
            self.original_clients.append(provider._client)
            provider._client = httpx.AsyncClient(
                base_url="https://api.hetzner.cloud/v1",
                transport=httpx.MockTransport(self.handler(account)),
            )
            self.providers[account] = provider
            self.adapters[account] = HetznerHourlyCloudProvider(
                provider=provider, account_id=account
            )
        self.selector = ProviderRouteSelector(repository=self, usage_readers={"hetzner": self})

    async def list_for_location(self, provider_key, location_id):
        return [
            ProviderRoute(
                provider_key,
                account,
                location_id,
                state=RouteState.ELIGIBLE_AVAILABLE,
                priority=priority,
                product_ids=("cx22",),
            )
            for priority, account in enumerate(self.counts)
        ]

    def accepts_new_orders(self, account):
        return account in self.counts

    async def server_usage(self, account):
        count = await self.providers[account].project_server_count()
        return AccountServerUsage(account, count, 5)

    def adapter_for(self, provider_key, credential_account_id):
        return self.adapters.get(credential_account_id)

    def handler(self, account):
        def handle(request):
            path = request.url.path
            if request.method == "POST":
                body = json.loads(request.content)
                self.posts.append((account, body))
                self.events.append((account, "post"))
                if account == "hz-a" and self.failure == "capacity":
                    return reply(
                        {
                            "error": {
                                "code": "resource_limit_exceeded",
                                "message": "Project resource limit exceeded",
                                "details": {"limits": [{"name": "project_limit"}]},
                            }
                        },
                        403,
                    )
                if account == "hz-a" and self.failure == "unknown":
                    raise httpx.ReadTimeout("lost acknowledgment", request=request)
                if account == "hz-a" and self.failure == "refused":
                    return reply(
                        {"error": {"code": "invalid_input", "message": "Invalid input"}}, 422
                    )
                return reply(
                    {
                        "server": {**server(body["name"]), "labels": body["labels"]},
                        "action": {"status": "running"},
                        "root_password": None,
                    },
                    201,
                )
            self.reads.append((account, path))
            if path == "/v1/pricing":
                return reply({"pricing": {"currency": "EUR"}})
            if path == "/v1/servers":
                if self.counts[account] is None:
                    return reply({"servers": [], "meta": {}})
                # Off and manually created resources count toward Project usage.
                return reply(
                    envelope(
                        "servers",
                        [
                            {"id": 1000 + i, "status": "off", "labels": {}}
                            for i in range(self.counts[account])
                        ],
                    )
                )
            if path == "/v1/server_types":
                plan = server_type()
                if account == "hz-b":
                    plan["prices"][0]["price_hourly"]["gross"] = self.b_rate
                return reply(envelope("server_types", [plan]))
            if path == "/v1/images":
                selected = image()
                if account == "hz-b":
                    selected["id"] = int(self.b_image)
                return reply(envelope("images", [selected]))
            if path == "/v1/locations":
                return reply(envelope("locations", [{"id": 1, "name": "fsn1"}]))
            raise AssertionError((account, request.method, path))

        return handle

    async def setup(self):
        for client in self.original_clients:
            await client.aclose()
        offer = replace(
            _offer(location_id="fsn1", product_id="cx22", provider_account_id="hz-a"),
            provider_key="hetzner",
            provider_cost_minor=1,
            provider_cost_currency="EUR",
            disk_gb=40,
            billing_parameters={"provider_hourly_rate": "0.0075"},
            technical_metadata={"storage_type": "LOCAL", "storage_types": ["LOCAL"]},
        )
        priced = await CatalogOfferPricer(_UsdRates(Decimal("1.17")), "USD").price_auto(
            offer, PricingPolicy(mode="markup", markup_percent=25)
        )
        self.offer = replace(
            offer,
            selling_price_minor=priced.selling_price_minor,
            selling_currency=priced.selling_currency,
            pricing_metadata=dict(priced.pricing_metadata),
        )
        self.offers = FakeOffersRepo([self.offer])
        self.service = HourlyCloudService(
            server_repo=self.servers,
            offers_repo=self.offers,
            account_repo=FakeAccountRepo(),
            wallet_repo=FakeWalletRepo2(),
            snapshot_service=self.snapshots,
            operation_repo=self.ops,
            audit_repo=FakeAuditRepo(),
            cloud_resolver=self,
            fulfillment_routes=self.selector,
            create_attempts=self.receipts,
            credential_store=AsyncMock(),
            **hourly_money(),
        )
        return self

    async def create(self):
        return await self.service.create_instance(
            user=USER,
            offer_id=self.offer.id,
            image_id="100",
            image_label="Ubuntu 24.04",
            idempotency_key="hetzner-hourly-confirmation",
            expected_selling_price_minor=self.offer.selling_price_minor,
            expected_selling_currency=self.offer.selling_currency,
        )

    def receipt(self, server_id):
        return self.ops.ops[f"server-create:{server_id}"].provider_response["create_routing"]


@pytest.fixture
async def pool():
    instance = await Pool().setup()
    try:
        yield instance
    finally:
        for provider in instance.providers.values():
            await provider.close()


def frozen_contract(pool, server):
    snapshot = pool.snapshots.created[0][1]
    return (
        json.dumps(server.offer_fingerprint, sort_keys=True),
        deepcopy(snapshot),
        server.image_id,
        server.os,
    )


async def test_full_preferred_project_selects_b_before_any_mutation(pool):
    pool.counts["hz-a"] = 5
    result = await pool.create()
    assert result.server.credential_account_id == "hz-b"
    assert pool.posts == []
    assert await pool.service.process_server(result.server.id) == "provisioned"
    assert [account for account, body in pool.posts] == ["hz-b"]
    assert result.server.credential_account_id == "hz-b"
    assert result.server.offer_fingerprint["provider_account_id"] == "hz-a"


async def test_refusal_continues_same_contract_and_replay_after_pin_changes(pool):
    pool.failure = "capacity"
    result = await pool.create()
    frozen = frozen_contract(pool, result.server)
    fingerprint = result.server.offer_fingerprint
    assert fingerprint["fingerprint_version"] == 3
    assert fingerprint["fulfillment_policy"] == "capacity_failover"
    assert "credential_account_id" not in fingerprint
    snapshot = pool.snapshots.created[0][1]
    assert (
        snapshot.offer.provider_rate_exact,
        snapshot.offer.currency,
        snapshot.offer.cost_minor,
    ) == ("0.0075", "EUR", 1)
    assert (snapshot.selling_minor, snapshot.selling_currency) == (
        pool.offer.selling_price_minor,
        "USD",
    )
    assert (
        fingerprint["root_disk_size_gb"],
        fingerprint["root_disk_storage_type"],
        fingerprint["image_id"],
        fingerprint["product_id"],
        fingerprint["location_id"],
    ) == (40, "LOCAL", "100", "cx22", "fsn1")
    assert await pool.service.process_server(result.server.id) == "provisioned"
    assert frozen_contract(pool, result.server) == frozen
    assert [account for account, body in pool.posts] == ["hz-a", "hz-b"]
    assert pool.posts[0][1] == pool.posts[1][1]
    assert pool.events.index(("hz-a", "sent")) < pool.events.index(("hz-a", "post"))
    assert pool.events.index(("hz-a", "capacity_refused")) < pool.events.index(("hz-b", "sent"))
    assert pool.events.index(("hz-b", "sent")) < pool.events.index(("hz-b", "post"))
    assert [attempt["phase"] for attempt in pool.receipt(result.server.id)["attempts"]] == [
        "capacity_refused",
        "accepted",
    ]
    assert pool.receipt(result.server.id)["accepted_account_id"] == "hz-b"
    assert result.server.provider_server_id == "54321"
    assert len(pool.snapshots.created) == len(pool.servers.servers) == len(pool.ops.ops) == 1
    # Existing-key replay uses the accepted immutable contract, not today's offer.
    pool.offers._rows.clear()
    replay = await pool.create()
    assert replay.replayed and replay.server.id == result.server.id
    assert frozen_contract(pool, replay.server) == frozen
    assert await pool.service.process_server(result.server.id) in {
        "skipped",
        "attached",
        "provisioned",
    }
    assert len(pool.posts) == 2


async def test_unknown_stays_on_a_and_read_only_recovery_never_switches(pool):
    pool.failure = "unknown"
    result = await pool.create()
    frozen = frozen_contract(pool, result.server)
    assert await pool.service.process_server(result.server.id) == "outcome-unknown"
    assert [account for account, body in pool.posts] == ["hz-a"]
    assert result.server.credential_account_id == "hz-a"
    assert pool.receipt(result.server.id)["attempts"][-1]["phase"] == "outcome_unknown"
    pool.reads.clear()
    await pool.service.reconcile_server(result.server.id)
    assert pool.reads and all(account == "hz-a" for account, path in pool.reads)
    assert len(pool.posts) == 1
    assert frozen_contract(pool, result.server) == frozen


@pytest.mark.parametrize("unreadable", [False, True])
async def test_full_pool_and_unreadable_project_are_distinct_checkout_errors(pool, unreadable):
    pool.counts.update({"hz-a": 5, "hz-b": None if unreadable else 5})
    expected = HourlyProviderUnavailableError if unreadable else HourlyAccountCapacityError
    with pytest.raises(expected):
        await pool.create()
    assert pool.posts == []
    assert pool.servers.servers == {}
    assert pool.snapshots.created == []


@pytest.mark.parametrize("incompatible", ["image", "rate"])
async def test_alternative_must_independently_supply_frozen_image_and_native_rate(
    pool, incompatible
):
    pool.failure = "capacity"
    result = await pool.create()
    frozen = frozen_contract(pool, result.server)
    if incompatible == "image":
        pool.b_image = "101"
    else:
        pool.b_rate = "0.0076"  # Same rounded minor cost, different exact native price.
    await pool.service.process_server(result.server.id)
    assert [account for account, body in pool.posts] == ["hz-a"]
    assert result.server.provider_server_id is None
    assert frozen_contract(pool, result.server) == frozen
    assert len(pool.receipt(result.server.id)["attempts"]) == 1


async def test_generic_refusal_does_not_authorize_account_continuation(pool):
    pool.failure = "refused"
    result = await pool.create()
    await pool.service.process_server(result.server.id)
    assert [account for account, body in pool.posts] == ["hz-a"]
    assert pool.receipt(result.server.id)["attempts"][-1]["phase"] == "refused"


async def test_lost_refusal_commit_forbids_next_account_post(pool):
    pool.failure = "capacity"
    pool.receipts.fail_refusal = True
    result = await pool.create()
    try:
        await pool.service.process_server(result.server.id)
    except CreateAttemptConflict:
        pass
    assert [account for account, body in pool.posts] == ["hz-a"]
    assert pool.receipt(result.server.id)["attempts"][-1]["phase"] == "sent"


@pytest.mark.parametrize("version", [1, 2])
async def test_historical_contracts_remain_pinned_even_with_pool_registered(pool, version):
    # Obtain a genuine pinned contract through the same service, then emulate
    # its supported historical version without adding any routing receipt.
    pool.service._routes = None
    result = await pool.create()
    snapshot = pool.snapshots.created[0][1]
    fingerprint = deepcopy(result.server.offer_fingerprint)
    fingerprint["fingerprint_version"] = version
    if version == 1:
        fingerprint.pop("root_disk_size_gb", None)
        fingerprint.pop("root_disk_storage_type", None)
    result.server.offer_fingerprint = fingerprint
    pool.snapshots.created[0] = (
        result.server.id,
        replace(snapshot, offer_fingerprint=deepcopy(fingerprint)),
    )
    original_snapshot = pool.snapshots.created[0][1]
    _validate_hourly_contract(result.server, original_snapshot)
    result.server.credential_account_id = "hz-b"
    with pytest.raises(HourlyNotAvailableError):
        _validate_hourly_contract(result.server, original_snapshot)
    result.server.credential_account_id = "hz-a"
    pool.service._routes = pool.selector
    pool.failure = "capacity"
    await pool.service.process_server(result.server.id)
    assert all(account == "hz-a" for account, body in pool.posts)
    assert len(pool.posts) == (0 if version == 1 else 1)
    assert result.server.credential_account_id == "hz-a"
    assert "create_routing" not in (
        pool.ops.ops[f"server-create:{result.server.id}"].provider_response or {}
    )


async def test_v2_root_disk_remains_mandatory(pool):
    pool.service._routes = None
    result = await pool.create()
    snapshot = pool.snapshots.created[0][1]
    fingerprint = deepcopy(result.server.offer_fingerprint)
    fingerprint["fingerprint_version"] = 2
    fingerprint.pop("root_disk_size_gb", None)
    fingerprint.pop("root_disk_storage_type", None)
    result.server.offer_fingerprint = fingerprint
    snapshot = replace(snapshot, offer_fingerprint=deepcopy(fingerprint))
    with pytest.raises(HourlyNotAvailableError):
        _validate_hourly_contract(result.server, snapshot)


async def test_worker_rechecks_project_capacity_before_first_sent(pool):
    result = await pool.create()
    assert result.server.credential_account_id == "hz-a"
    pool.counts.update({"hz-a": 5, "hz-b": 5})
    await pool.service.process_server(result.server.id)
    assert pool.posts == []
    assert result.server.provider_server_id is None
    assert pool.receipt(result.server.id)["attempts"] == []


@pytest.mark.parametrize("incompatible", ["image", "rate"])
async def test_checkout_never_borrows_catalog_account_inputs_for_selected_b(pool, incompatible):
    pool.counts["hz-a"] = 5
    if incompatible == "image":
        pool.b_image = "101"
    else:
        pool.b_rate = "0.0076"
    with pytest.raises(HourlyNotAvailableError):
        await pool.create()
    assert pool.posts == []
    assert pool.servers.servers == {}


@pytest.mark.parametrize(
    "error",
    [
        ProviderCapacityError("local capacity observation is not a mutation refusal"),
        ProviderError("no authoritative response"),
    ],
)
async def test_unproven_mutation_errors_retain_unknown_identity_and_contract(pool, error):
    result = await pool.create()
    frozen = frozen_contract(pool, result.server)
    attempted = []

    async def unverifiable_create(**kwargs):
        attempted.append(kwargs)
        raise error

    pool.adapters["hz-a"].create_instance = unverifiable_create
    assert await pool.service.process_server(result.server.id) == "outcome-unknown"
    await pool.service.process_server(result.server.id)
    assert len(attempted) == 1 and pool.posts == []
    assert pool.receipt(result.server.id)["attempts"][-1]["phase"] == "outcome_unknown"
    assert result.server.credential_account_id == "hz-a"
    assert result.server.provider_server_id is None
    assert frozen_contract(pool, result.server) == frozen


async def test_hourly_polling_does_not_quarantine_legacy_or_monthly_intents(pool):
    result = await pool.create()
    legacy = replace(
        result.server,
        id=uuid4(),
        offer_fingerprint={},
        idempotency_key="legacy-bot-intent",
    )
    monthly = replace(
        result.server,
        id=uuid4(),
        billing_model="prepaid_monthly_fixed",
        idempotency_key="monthly-bot-intent",
    )
    pool.servers.servers[legacy.id] = legacy
    pool.servers.servers[monthly.id] = monthly
    for requested in await pool.service.servers_requested():
        await pool.service.process_server(requested.id)
    assert legacy.state is monthly.state is ServerLifecycleState.REQUESTED
    assert legacy.provider_server_id is monthly.provider_server_id is None
    assert result.server.provider_server_id == "54321"
    assert [account for account, body in pool.posts] == ["hz-a"]
    assert len(pool.ops.ops) == 1


@pytest.mark.parametrize("dispatch", ["process", "reconcile"])
async def test_terminal_refusal_crash_recovers_failed_without_another_mutation(
    pool, monkeypatch, dispatch
):
    pool.failure = "refused"
    result = await pool.create()
    save_outcome = pool.receipts.save_outcome

    async def crash_after_refusal(*args, **kwargs):
        if args[2] is OperationStatus.FAILED:
            raise RuntimeError("crash after durable refusal")
        return await save_outcome(*args, **kwargs)

    monkeypatch.setattr(pool.receipts, "save_outcome", crash_after_refusal)
    with pytest.raises(RuntimeError, match="crash after durable refusal"):
        await pool.service.process_server(result.server.id)
    operation = next(iter(pool.ops.ops.values()))
    generation = operation.attempts
    assert pool.receipt(result.server.id)["attempts"][-1]["phase"] == "refused"
    assert result.server.state is ServerLifecycleState.REQUESTED
    monkeypatch.setattr(pool.receipts, "save_outcome", save_outcome)
    operation.updated_at = datetime.now(UTC) - timedelta(hours=1)
    if dispatch == "process":
        assert await pool.service.process_server(result.server.id) == "failed"
    else:
        assert await pool.service.reconcile_server(result.server.id) == "failed"
    assert [account for account, _ in pool.posts] == ["hz-a"]
    assert operation.attempts > generation
    assert operation.status is OperationStatus.FAILED
    assert result.server.state is ServerLifecycleState.ERROR
    assert result.server.credential_account_id == "hz-a"
    assert result.server.provider_server_id is None
    assert pool.receipt(result.server.id)["attempts"][-1]["phase"] == "refused"
