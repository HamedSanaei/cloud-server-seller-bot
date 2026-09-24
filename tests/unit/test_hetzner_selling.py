"""Hetzner end-to-end SELLING tests: offer sync, direct-create safety, storefront.

Acceptance (provider-neutral storefront task):
- real Hetzner server types become SellableOffer rows with provider COST only:
  a newly discovered offer is safe by default (never enabled, never priced),
- the location-scoped LIST endpoint is the only catalog source; the per-id
  detail endpoint is never required,
- one failing location never disturbs the others and never retires their offers,
- a direct-create provider is buyable in the storefront WITHOUT implementing
  the ordering port, and its OS options come from its own capability,
- an ambiguous create POST is NEVER retried: it is recorded OUTCOME_UNKNOWN and
  resolved READ-ONLY through the platform operation identity,
- activation captures the hold exactly once and persists the provider server id,
- nothing here performs a live provider call (all transports are fakes).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.checkout.service import OfferCatalogViewService
from cloud_platform.modules.compute.domain import (
    BILLING_MODEL_PREPAID_MONTHLY,
    CloudServer,
    ServerLifecycleState,
)
from cloud_platform.modules.markets.domain import ProviderCatalog
from cloud_platform.modules.offers.domain import SellableOffer
from cloud_platform.modules.operations.domain import (
    Operation,
    OperationStatus,
    OperationType,
)
from cloud_platform.modules.orders.domain import OrderStatus, ProviderOrder, SettlementStatus
from cloud_platform.modules.orders.service import OrderWorker, SettlementVerdict
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    OrderRecoveryResult,
    OrderRecoveryVerdict,
    ProviderServer,
    server_recovery_support_of,
)
from cloud_platform.providers.errors import (
    ProviderConflict,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.client import HetznerCloudProvider
from cloud_platform.providers.hetzner.sync import HetznerCatalogSyncer
from cloud_platform.providers.registry import ProviderRegistry

USER_ID = uuid4()
WALLET_ID = uuid4()
SERVER_ID = uuid4()
ORDER_ID = uuid4()
OP_KEY = f"order-create:{SERVER_ID}"
HOLD_KEY = "leaseweb-order:bot-monthly:abc"
PROVIDER_KEY = "hetzner"


# ---------------------------------------------------------------------------
# A. Offer sync: provider cost only, list endpoint authoritative
# ---------------------------------------------------------------------------


class _RecordingOfferRepo:
    """Records offer writes; the real repo is exercised by its own tests."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []
        self.marked: tuple[str, set[tuple[str, str]]] | None = None

    async def upsert_from_provider(
        self,
        *,
        provider_key: str,
        product_id: str,
        location_id: str,
        update: Any,
        provider_account_id: str | None = None,
    ) -> None:
        self.writes.append(
            {
                "provider_key": provider_key,
                "product_id": product_id,
                "location_id": location_id,
                "update": update,
            }
        )

    async def mark_unavailable(self, provider_key: str, available: set[tuple[str, str]]) -> int:
        self.marked = (provider_key, available)
        return 3

    def by_location(self, location_id: str) -> list[dict[str, Any]]:
        return [w for w in self.writes if w["location_id"] == location_id]


def _server_type(
    name: str,
    *,
    location: str,
    monthly: str,
    type_id: int = 1,
    cores: int = 2,
    memory: str = "4.0",
    disk: int = 40,
    traffic_bytes: int = 20 * 2**40,
) -> dict[str, Any]:
    return {
        "id": type_id,
        "name": name,
        "cores": cores,
        "memory": memory,
        "disk": disk,
        "included_traffic": traffic_bytes,
        "prices": [
            {
                "location": location,
                "monthly": {"gross": monthly},
                "hourly": {"gross": "0.0070"},
            }
        ],
    }


@pytest.fixture
def offer_repo() -> _RecordingOfferRepo:
    return _RecordingOfferRepo()


@pytest.fixture
def syncer(offer_repo: _RecordingOfferRepo) -> HetznerCatalogSyncer:
    with patch("cloud_platform.providers.hetzner.sync.get_settings") as settings:
        settings.return_value.hetzner_api_token = "test-token"
        instance = HetznerCatalogSyncer(session_factory=lambda: None, token="test-token")
    instance._client = AsyncMock()
    with patch(
        "cloud_platform.providers.hetzner.sync.SqlAlchemySellableOfferRepository",
        lambda _factory, **kwargs: offer_repo,
    ):
        yield instance


def _responses(
    locations: dict[str, list[dict[str, Any]]],
    *,
    failing: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Location page first, then one server-type page per location (in order)."""
    payloads: list[dict[str, Any]] = [
        {"locations": [{"id": loc} for loc in locations], "meta": {"pagination": {}}}
    ]
    for location_id, types in locations.items():
        if failing and location_id in failing:
            payloads.append(ProviderUnavailable("boom"))  # type: ignore[arg-type]
        else:
            payloads.append({"server_types": types, "meta": {"pagination": {}}})
    return payloads


@pytest.mark.asyncio
async def test_one_offer_per_location_with_provider_cost_only(
    syncer: HetznerCatalogSyncer, offer_repo: _RecordingOfferRepo
) -> None:
    """Three locations merge into ONE catalog; each is its own sellable offer."""
    syncer._request = AsyncMock(
        side_effect=_responses(
            {
                "region-a1": [_server_type("cx-test", location="region-a1", monthly="4.49")],
                "region-b2": [_server_type("cx-test", location="region-b2", monthly="4.49")],
                "region-c3": [_server_type("cx-test", location="region-c3", monthly="4.79")],
            }
        )
    )
    result = await syncer.sync_offers()

    assert result.offers_written == 3
    assert [r.products for r in result.locations] == [1, 1, 1]
    refs = {(w["product_id"], w["location_id"]) for w in offer_repo.writes}
    assert refs == {
        ("cx-test", "region-a1"),
        ("cx-test", "region-b2"),
        ("cx-test", "region-c3"),
    }
    assert offer_repo.marked is not None
    assert offer_repo.marked[0] == PROVIDER_KEY
    assert offer_repo.marked[1] == refs


@pytest.mark.asyncio
async def test_same_product_in_two_locations_is_two_offers(
    syncer: HetznerCatalogSyncer, offer_repo: _RecordingOfferRepo
) -> None:
    """Deduplicating by product_id alone would hide a sellable location."""
    syncer._request = AsyncMock(
        side_effect=_responses(
            {
                "region-a1": [_server_type("cx-test", location="region-a1", monthly="4.49")],
                "region-b2": [_server_type("cx-test", location="region-b2", monthly="4.49")],
            }
        )
    )
    await syncer.sync_offers()
    assert len(offer_repo.by_location("region-a1")) == 1
    assert len(offer_repo.by_location("region-b2")) == 1


@pytest.mark.asyncio
async def test_price_is_parsed_from_decimal_to_minor_units(
    syncer: HetznerCatalogSyncer, offer_repo: _RecordingOfferRepo
) -> None:
    """4.49 EUR must become exactly 449 minor units — no float anywhere."""
    syncer._request = AsyncMock(
        side_effect=_responses(
            {"region-a1": [_server_type("cx-test", location="region-a1", monthly="4.49")]}
        )
    )
    await syncer.sync_offers()
    update = offer_repo.writes[0]["update"]
    assert update.provider_cost_minor == 449
    assert update.provider_cost_currency == "EUR"
    assert isinstance(update.provider_cost_minor, int)


@pytest.mark.asyncio
async def test_new_offers_are_safe_by_default(
    syncer: HetznerCatalogSyncer, offer_repo: _RecordingOfferRepo
) -> None:
    """A catalog refresh must never put a product on sale."""
    syncer._request = AsyncMock(
        side_effect=_responses(
            {"region-a1": [_server_type("cx-test", location="region-a1", monthly="4.49")]}
        )
    )
    await syncer.sync_offers()
    update = offer_repo.writes[0]["update"]
    # The sync writes provider facts only: no selling price, no enable flag.
    assert not hasattr(update, "selling_price_minor")
    assert update.provider_available is True
    assert update.billing_parameters["monthly_price_source"] == "server_types.location"


@pytest.mark.asyncio
async def test_failing_location_is_isolated_and_never_retires_offers(
    syncer: HetznerCatalogSyncer, offer_repo: _RecordingOfferRepo
) -> None:
    """One broken location must not remove the others, nor mark them gone."""
    syncer._request = AsyncMock(
        side_effect=_responses(
            {
                "region-a1": [_server_type("cx-test", location="region-a1", monthly="4.49")],
                "region-b2": [_server_type("cx-test", location="region-b2", monthly="4.49")],
                "region-c3": [_server_type("cx-test", location="region-c3", monthly="4.49")],
            },
            failing={"region-b2"},
        )
    )
    result = await syncer.sync_offers()

    assert result.offers_written == 2
    assert {r.location_id for r in result.locations if r.error} == {"region-b2"}
    assert offer_repo.by_location("region-a1")
    assert offer_repo.by_location("region-c3")
    # Availability reconciliation is skipped: a partial view must not retire.
    assert offer_repo.marked is None
    assert any("mark_unavailable" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_products_without_a_monthly_price_are_not_invented(
    syncer: HetznerCatalogSyncer, offer_repo: _RecordingOfferRepo
) -> None:
    """No provider price => no offer row, just a warning."""
    item = _server_type("cx-test", location="region-a1", monthly="4.49")
    item["prices"] = []
    syncer._request = AsyncMock(side_effect=_responses({"region-a1": [item]}))
    result = await syncer.sync_offers()
    assert result.offers_written == 0
    assert any("no monthly price" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_catalog_membership_comes_from_the_list_endpoint(
    syncer: HetznerCatalogSyncer,
) -> None:
    """The list endpoint is authoritative; the per-id detail read is not used."""
    syncer._request = AsyncMock(
        side_effect=_responses(
            {"region-a1": [_server_type("cx-test", location="region-a1", monthly="4.49")]}
        )
    )
    await syncer.sync_offers()
    paths = [call.args[1] for call in syncer._request.await_args_list]
    assert paths == ["/locations", "/server_types"]
    for call in syncer._request.await_args_list:
        if call.args[1] == "/server_types":
            assert call.kwargs["params"]["location"] == "region-a1"


# ---------------------------------------------------------------------------
# B. Hetzner adapter: ambiguity, read-only recovery, options, validation
# ---------------------------------------------------------------------------


class _FakeHttp:
    def __init__(self, handler: Any) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((method, path, kwargs))
        return self._handler(method, path, kwargs)

    async def aclose(self) -> None:
        return None


def _provider(handler: Any) -> HetznerCloudProvider:
    provider = HetznerCloudProvider(token="tetra-not-a-real-token")
    provider._client = _FakeHttp(handler)  # type: ignore[assignment]
    return provider


def _json(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=payload, headers={})


def _create_request() -> CreateServerRequest:
    return CreateServerRequest(
        name="srv-test",
        plan_id="cx-test",
        image_id="ubuntu-24.04",
        location_id="region-a1",
        labels={"platform_server_id": str(SERVER_ID)},
    )


@pytest.mark.asyncio
async def test_create_server_ambiguity_is_never_retryable() -> None:
    """A timeout/drop after the POST must not look like a safe retry."""
    provider = _provider(lambda *_: (_ for _ in ()).throw(httpx.ReadTimeout("dropped")))

    with pytest.raises(ProviderOutcomeUnknown):
        await provider.create_server(_create_request(), IdempotencyKey(OP_KEY))


@pytest.mark.asyncio
async def test_create_server_success_sends_the_operation_label() -> None:
    captured: dict[str, Any] = {}

    def handler(method: str, path: str, kwargs: dict[str, Any]) -> httpx.Response:
        captured.update(kwargs["json"])
        return _json(
            201,
            {
                "server": {
                    "id": 42,
                    "name": "srv-test",
                    "status": "initializing",
                    "public_net": {"ipv4": {"ip": "203.0.113.7"}, "ipv6": {"ip": "2001:db8::1"}},
                }
            },
        )

    provider = _provider(handler)
    server = await provider.create_server(_create_request(), IdempotencyKey(OP_KEY))

    assert server.id == "42"
    assert captured["labels"]["platform-operation"] == OP_KEY
    assert server.ipv4 == "203.0.113.7"


@pytest.mark.asyncio
async def test_recovery_requires_exactly_one_provable_server() -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> httpx.Response:
        assert kwargs["params"]["label_selector"] == f"platform-operation={OP_KEY}"
        return _json(
            200,
            {
                "servers": [
                    {
                        "id": 7,
                        "name": "a",
                        "status": "running",
                        "labels": {"platform-operation": OP_KEY},
                    }
                ]
            },
        )

    recovery = server_recovery_support_of(_provider(handler))
    assert recovery is not None
    result = await recovery.recover_server_by_operation(OP_KEY)
    assert result.verdict is OrderRecoveryVerdict.MATCHED
    assert result.provider_order_id == "7"


@pytest.mark.asyncio
async def test_recovery_reports_no_match_and_ambiguity() -> None:
    empty = server_recovery_support_of(_provider(lambda *_: _json(200, {"servers": []})))
    several = server_recovery_support_of(
        _provider(
            lambda *_: _json(
                200,
                {
                    "servers": [
                        {
                            "id": 1,
                            "name": "a",
                            "status": "running",
                            "labels": {"platform-operation": OP_KEY},
                        },
                        {
                            "id": 2,
                            "name": "b",
                            "status": "running",
                            "labels": {"platform-operation": OP_KEY},
                        },
                    ]
                },
            )
        )
    )
    assert empty is not None and several is not None
    assert (
        await empty.recover_server_by_operation(OP_KEY)
    ).verdict is OrderRecoveryVerdict.NO_MATCH
    ambiguous = await several.recover_server_by_operation(OP_KEY)
    assert ambiguous.verdict is OrderRecoveryVerdict.AMBIGUOUS
    assert ambiguous.candidate_count == 2


@pytest.mark.asyncio
async def test_recovery_scan_failure_is_transient_not_ambiguous() -> None:
    """A 5xx during the scan must never be read as 'server does not exist'."""
    recovery = server_recovery_support_of(
        _provider(lambda *_: _json(503, {"error": {"message": "down"}}))
    )
    assert recovery is not None
    result = await recovery.recover_server_by_operation(OP_KEY)
    assert result.verdict is OrderRecoveryVerdict.SCAN_FAILED
    assert result.provider_order_id is None


@pytest.mark.asyncio
async def test_os_options_only_offer_creatable_system_images() -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> httpx.Response:
        if path == "/server_types/cx-test":
            return _json(200, {"server_type": {"id": 1, "architecture": "x86"}})
        assert kwargs["params"]["type"] == "system"
        assert kwargs["params"]["include_deprecated"] == "false"
        assert kwargs["params"]["architecture"] == "x86"
        return _json(
            200,
            {
                "images": [
                    {"id": 11, "name": "ubuntu-24.04"},
                    {"id": 12, "name": "debian-12"},
                ]
            },
        )

    options = await _provider(handler).get_os_options("region-a1", "cx-test")
    assert [option.name for option in options] == ["debian-12", "ubuntu-24.04"]
    assert options[0].image_id == "12"


@pytest.mark.asyncio
async def test_checkout_validation_rejects_price_drift_and_missing_image() -> None:
    def server_types(**_: Any) -> httpx.Response:
        return _json(
            200,
            {
                "server_types": [
                    _server_type("cx-test", location="region-a1", monthly="9.99"),
                ]
            },
        )

    images = _json(200, {"images": []})
    provider = _provider(
        lambda method, path, kwargs: server_types() if path == "/server_types" else images
    )

    with pytest.raises(ProviderConflict):
        await provider.validate_offer_for_checkout(
            location_id="region-a1",
            product_id="cx-test",
            os_name="ubuntu-24.04",
            expected_cost_minor=449,
            currency="EUR",
        )
    # Same provider, matching price: now the image is the problem.
    with pytest.raises(ProviderNotFound):
        await provider.validate_offer_for_checkout(
            location_id="region-a1",
            product_id="cx-test",
            os_name="ubuntu-24.04",
            expected_cost_minor=999,
            currency="EUR",
        )


# ---------------------------------------------------------------------------
# C. Storefront: a direct-create provider is buyable WITHOUT ordering
# ---------------------------------------------------------------------------


class _DirectCreateProvider:
    """A provider that provisions directly (no ordering port) — like Hetzner."""

    key = PROVIDER_KEY
    capabilities = frozenset({Capability.COMPUTE})

    def __init__(self) -> None:
        self.os_calls: list[tuple[str, str]] = []

    async def list_locations(self) -> list[Any]:
        return []

    async def list_plans(self) -> list[Any]:
        return []

    async def list_images(self) -> list[Any]:
        return []

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        return None

    async def list_servers(self) -> list[ProviderServer]:
        return []

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer:
        raise AssertionError("storefront must never create a server")

    async def delete_server(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        raise AssertionError("storefront must never delete a server")

    async def get_os_options(self, location_id: str, product_id: str) -> list[Any]:
        from cloud_platform.providers.base import OfferOsOption

        self.os_calls.append((location_id, product_id))
        return [
            OfferOsOption(name="ubuntu-24.04", image_id="11"),
            OfferOsOption(name="debian-12", image_id="12"),
        ]

    async def validate_offer_for_checkout(self, **_: Any) -> None:
        return None


class _OfferRepo:
    def __init__(self, offers: list[SellableOffer]) -> None:
        self._offers = offers

    async def get(self, offer_id: UUID) -> SellableOffer | None:
        return next((o for o in self._offers if o.id == offer_id), None)

    async def list_all(self) -> list[SellableOffer]:
        return list(self._offers)

    async def list_sellable(self, provider_key: str | None = None) -> list[SellableOffer]:
        return [o for o in self._offers if provider_key is None or o.provider_key == provider_key]


class _WalletRepo:
    async def get(self, user_id: UUID) -> Any:
        return None


def _offer(location_id: str = "region-a1") -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key=PROVIDER_KEY,
        product_id="cx-test",
        location_id=location_id,
        name="CX Test",
        vcpu=2,
        ram_gb=4,
        disk_gb=40,
        traffic="20 TB",
        provider_cost_minor=449,
        provider_cost_currency="EUR",
        selling_price_minor=999,
        selling_currency="EUR",
        billing_parameters={},
        provider_available=True,
        enabled=True,
    )


def _view_service(offers: list[SellableOffer], provider: Any) -> OfferCatalogViewService:
    registry = ProviderRegistry()
    registry.register(provider)
    return OfferCatalogViewService(
        offers_repo=_OfferRepo(offers),
        provider_registry=registry,
        wallet_repo=_WalletRepo(),
        signing_key="k" * 32,
        market_catalog=ProviderCatalog(
            markets={PROVIDER_KEY: "foreign"}, display_names={PROVIDER_KEY: "Hetzner"}
        ),
    )


@pytest.mark.asyncio
async def test_direct_create_provider_is_buyable_without_an_ordering_port() -> None:
    """Hetzner sells in the storefront even though it has no place_order."""
    service = _view_service([_offer()], _DirectCreateProvider())
    views, _back = await service.providers_screen("foreign")
    assert [view.provider_key for view in views] == [PROVIDER_KEY]
    assert views[0].buyable is True
    assert views[0].select_callback is not None
    assert views[0].display_name == "Hetzner"


@pytest.mark.asyncio
async def test_os_options_come_from_the_provider_capability() -> None:
    """OS listing is provider-neutral: it never requires the ordering port."""
    provider = _DirectCreateProvider()
    service = _view_service([_offer()], provider)
    options = await service.os_options(_offer())
    assert [option.name for option in options] == ["ubuntu-24.04", "debian-12"]
    assert provider.os_calls == [("region-a1", "cx-test")]


@pytest.mark.asyncio
async def test_product_card_groups_locations_under_one_product() -> None:
    """The same product in several locations is ONE card with many locations."""
    service = _view_service(
        [_offer("region-a1"), _offer("region-b2"), _offer("region-c3")],
        _DirectCreateProvider(),
    )
    cards, _back, _cancel = await service.products_screen(PROVIDER_KEY)
    assert len(cards) == 1
    assert cards[0].locations == ("region-a1", "region-b2", "region-c3")
    assert cards[0].monthly_price_minor == 999


# ---------------------------------------------------------------------------
# D. Direct-create worker: one POST, one hold capture, no blind retry
# ---------------------------------------------------------------------------


class _DirectCreateCloudProvider(_DirectCreateProvider):
    """Records create calls; can be made ambiguous after transmitting."""

    def __init__(self, *, outcome: str = "ok") -> None:
        super().__init__()
        self.outcome = outcome
        self.posts: list[IdempotencyKey] = []
        self.recovery_calls = 0
        self.recovery = OrderRecoveryResult(verdict=OrderRecoveryVerdict.NO_MATCH, reason="none")

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer:
        self.posts.append(idempotency_key)
        if self.outcome == "ambiguous":
            raise ProviderOutcomeUnknown("dropped after transmission")
        return ProviderServer(
            id="4242",
            name=request.name,
            status="initializing",
            ipv4="203.0.113.7",
            ipv6="2001:db8::1",
        )

    async def recover_server_by_operation(
        self, operation_key: str, since: datetime | None = None
    ) -> OrderRecoveryResult:
        self.recovery_calls += 1
        return self.recovery


def _worker_order() -> ProviderOrder:
    return ProviderOrder(
        id=ORDER_ID,
        server_id=SERVER_ID,
        offer_id=SERVER_ID,  # any offer id; the repo returns the fixture offer
        operation_key=OP_KEY,
        provider_key=PROVIDER_KEY,
        status=OrderStatus.PENDING_SUBMIT,
        provider_cost_minor=449,
        provider_cost_currency="EUR",
        settlement_status=SettlementStatus.PENDING,
    )


def _worker_server() -> CloudServer:
    return CloudServer(
        id=SERVER_ID,
        user_id=USER_ID,
        provider_key=PROVIDER_KEY,
        provider_account_id=None,
        state=ServerLifecycleState.REQUESTED,
        billing_model=BILLING_MODEL_PREPAID_MONTHLY,
        os="ubuntu-24.04",
        idempotency_key="bot-monthly:abc",
    )


def _claimed_operation() -> Operation:
    now = datetime.now(UTC)
    return Operation(
        id=uuid4(),
        operation_key=OP_KEY,
        operation_type=OperationType.SERVER_CREATE,
        resource_type="server",
        resource_id=SERVER_ID,
        provider_key=PROVIDER_KEY,
        status=OperationStatus.PENDING,
        created_at=now,
        updated_at=now,
    )


class _Bundle:
    """Minimal collaborators for one direct-create worker pass."""

    def __init__(self, provider: _DirectCreateCloudProvider, offer: SellableOffer) -> None:
        self.provider = provider
        self.offer = offer
        self.server = _worker_server()
        self.order = _worker_order()
        self.operation = _claimed_operation()
        self.server_saves: list[CloudServer] = []
        self.order_saves: list[ProviderOrder] = []

        registry = ProviderRegistry()
        registry.register(provider)

        servers = MagicMock()
        servers.list_requested_prepaid = AsyncMock(return_value=[self.server])
        servers.save = AsyncMock(side_effect=self._save_server)
        servers.get = AsyncMock(return_value=self.server)

        orders = MagicMock()
        orders.get_by_server = AsyncMock(return_value=self.order)
        orders.save = AsyncMock(side_effect=self._save_order)
        orders.get = AsyncMock(return_value=self.order)

        offers = MagicMock()
        offers.get = AsyncMock(return_value=self.offer)

        self.claimed = copy.deepcopy(self.operation)
        self.claimed.mark_in_flight()
        ops = MagicMock()
        ops.get_by_key = AsyncMock(return_value=self.operation)
        ops.claim = AsyncMock(return_value=self.claimed)
        ops.save = AsyncMock()

        audit = MagicMock()
        audit.append = AsyncMock()
        audit.record = AsyncMock()

        settlement = MagicMock()
        settlement.ensure_order_payment_settled = AsyncMock(return_value=SettlementVerdict.SETTLED)
        self.settlement = settlement

        notifier = MagicMock()
        notifier.deliver = AsyncMock()

        self.worker = OrderWorker(
            server_repo=servers,
            offers_repo=offers,
            orders_repo=orders,
            operation_repo=ops,
            wallet_repo=MagicMock(),
            hold_repo=MagicMock(),
            hold_service=MagicMock(),
            ledger_repo=MagicMock(),
            audit_repo=audit,
            provider_registry=registry,
            renewal_repo=MagicMock(),
            delivery_notifier=notifier,
            clock=None,
            event_sink=None,
            user_repo=None,
            # The settlement barrier has its own tests; here we assert HOW
            # OFTEN the direct-create path invokes it (never a provider call).
            settlement=settlement,
        )
        self.ops = ops

    async def _save_server(self, server: CloudServer) -> CloudServer:
        self.server = server
        self.server_saves.append(server)
        return server

    async def _save_order(self, order: ProviderOrder) -> ProviderOrder:
        self.order = order
        self.order_saves.append(order)
        return order


@pytest.mark.asyncio
async def test_direct_create_posts_once_and_persists_the_server_before_money() -> None:
    provider = _DirectCreateCloudProvider()
    bundle = _Bundle(provider, _offer())
    await bundle.worker.process_pending()

    assert provider.posts and len(provider.posts) == 1
    assert provider.posts[0].value == OP_KEY
    assert bundle.server.provider_server_id == "4242"
    assert bundle.server.ipv4 == "203.0.113.7"
    assert bundle.server.state is ServerLifecycleState.PROVISIONING
    # Settlement runs exactly once for this accepted create.
    assert bundle.settlement.ensure_order_payment_settled.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_direct_create_never_posts_twice() -> None:
    """A dropped create is recorded unknown — never retried automatically."""
    provider = _DirectCreateCloudProvider(outcome="ambiguous")
    bundle = _Bundle(provider, _offer())

    await bundle.worker.process_pending()
    assert len(provider.posts) == 1
    assert provider.recovery_calls == 1
    assert bundle.order.status is OrderStatus.OUTCOME_UNKNOWN
    assert bundle.server.provider_server_id is None
    # The wallet hold is untouched: nothing was captured.
    assert bundle.settlement.ensure_order_payment_settled.await_count == 0

    # A second pass must not POST again either.
    await bundle.worker.process_pending()
    assert len(provider.posts) == 1


@pytest.mark.asyncio
async def test_ambiguous_direct_create_claims_a_recovered_server_readonly() -> None:
    """When the label scan PROVES ownership, attach it instead of escalating."""
    provider = _DirectCreateCloudProvider(outcome="ambiguous")
    provider.recovery = OrderRecoveryResult(
        verdict=OrderRecoveryVerdict.MATCHED,
        provider_order_id="4242",
        candidate_count=1,
        reason="platform-operation",
    )
    bundle = _Bundle(provider, _offer())
    bundle.provider.get_server = AsyncMock(  # type: ignore[method-assign]
        return_value=ProviderServer(id="4242", name="srv", status="initializing")
    )

    await bundle.worker.process_pending()

    assert len(provider.posts) == 1  # never a second POST
    assert provider.recovery_calls == 1
    assert bundle.server.provider_server_id == "4242"
    assert bundle.settlement.ensure_order_payment_settled.await_count == 1


@pytest.mark.asyncio
async def test_direct_create_price_change_fails_before_any_post() -> None:
    """A moved provider price must stop the purchase with ZERO provider writes."""

    class _Drifting(_DirectCreateCloudProvider):
        async def validate_offer_for_checkout(self, **_: Any) -> None:
            raise ProviderConflict("provider price changed")

    provider = _Drifting()
    bundle = _Bundle(provider, _offer())
    await bundle.worker.process_pending()

    assert provider.posts == []
    assert bundle.settlement.ensure_order_payment_settled.await_count == 0
    assert bundle.server.provider_server_id is None
