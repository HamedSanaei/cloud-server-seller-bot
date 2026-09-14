"""Mocked contract tests for the read-only Leaseweb **Account Orders** API.

``GET /account/v1/orders``
``GET /account/v1/orders/{Id}``

These two operations are what let the platform attach a delivered VPS to the
exact order that bought it. The suite proves:

- the documented query parameters and the documented response shape;
- the order exposes ``equipmentId`` per service line, which is the ONLY
  provider-supported identity for the delivered VPS (never a similarity
  heuristic on plan/price/location);
- status/product values that Leaseweb introduces later are preserved;
- the client is strictly read-only (no mutating method exists), so a
  reconciliation loop cannot create an order;
- money is parsed as :class:`decimal.Decimal`.
"""

from __future__ import annotations

from datetime import UTC
from decimal import Decimal

import httpx
import pytest
import respx

from cloud_platform.providers.leaseweb.errors import (
    LeasewebNotFoundError,
    LeasewebServerError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.orders_api import (
    AccountOrder,
    LeaseWebAccountOrdersApi,
    ServiceStatus,
)
from cloud_platform.providers.leaseweb.transport import LeasewebTransport, Throttle

KEY = "test-leaseweb-key"
BASE = "https://api.test"
ORDER_ID = "LS-ORD-1"


def _no_sleep() -> Throttle:
    async def wait(_seconds: float) -> None:
        return None

    return Throttle(max_rps=100_000.0, wait=wait)


def _api() -> LeaseWebAccountOrdersApi:
    return LeaseWebAccountOrdersApi(LeasewebTransport(KEY, BASE, throttle=_no_sleep()))


def _meta(total: int, offset: int = 0, limit: int = 20) -> dict[str, int]:
    return {"totalCount": total, "offset": offset, "limit": limit}


def _order(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": ORDER_ID,
        "contractId": "CT-1",
        "createdAt": "2024-08-23T11:00:00Z",
        "type": "NEW_ORDER",
        "origin": "WEBSITE",
        "quotation": "QT-9",
        "services": [
            {
                "id": "SVC-1",
                "productId": "VIRTUAL_SERVER",
                "status": "TO_BE_PROVISIONED",
                "deliveryEstimate": "2024-08-23T12:00:00Z",
                "pricePerFrequency": "9.99",
                "currency": "EUR",
                "contractTerm": "1_MONTH",
                "billingCycle": "1_MONTH",
            }
        ],
    }
    payload.update(overrides)
    return payload


PROVISIONED_ORDER = _order(
    services=[
        {
            "id": "SVC-1",
            "productId": "VIRTUAL_SERVER",
            "status": "ACTIVE",
            "deliveryEstimate": "2024-08-23T12:00:00Z",
            "equipmentId": "VPS02_1",
            "pricePerFrequency": "9.99",
            "currency": "EUR",
            "contractTerm": "1_MONTH",
            "billingCycle": "1_MONTH",
        },
        {
            "id": "SVC-2",
            "productId": "IP_POOL",
            "status": "ACTIVE",
            "pricePerFrequency": "0.00",
            "currency": "EUR",
        },
    ]
)


class TestListOrders:
    """``GET /account/v1/orders``."""

    @respx.mock
    async def test_list_orders_paginates(self) -> None:
        route = respx.get(f"{BASE}/account/v1/orders").mock(
            return_value=httpx.Response(200, json={"orders": [_order()], "_metadata": _meta(1)})
        )
        api = _api()
        try:
            page = await api.list_orders(limit=20, offset=0)
        finally:
            await api.aclose()
        request = route.calls.last.request
        assert request.method == "GET"
        assert request.headers["X-LSW-Auth"] == KEY
        assert dict(request.url.params) == {"limit": "20", "offset": "0"}
        assert page.metadata is not None and page.metadata.total_count == 1
        order = page.items[0]
        assert order.id == ORDER_ID
        assert order.contract_id == "CT-1"
        assert order.quotation == "QT-9"
        assert order.created_at_dt is not None
        assert order.created_at_dt.tzinfo == UTC
        assert order.first_equipment_id() is None  # not provisioned yet

    @respx.mock
    async def test_list_orders_without_filters_sends_no_query(self) -> None:
        route = respx.get(f"{BASE}/account/v1/orders").mock(
            return_value=httpx.Response(200, json={"orders": [], "_metadata": _meta(0)})
        )
        api = _api()
        try:
            page = await api.list_orders()
        finally:
            await api.aclose()
        assert not route.calls.last.request.url.query
        assert page.items == []

    @respx.mock
    async def test_list_orders_server_error_is_retryable_but_never_mutating(self) -> None:
        route = respx.get(f"{BASE}/account/v1/orders").mock(
            return_value=httpx.Response(500, json={"errorMessage": "boom"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebServerError):
                await api.list_orders()
        finally:
            await api.aclose()
        assert route.call_count == 1


class TestGetOrder:
    """``GET /account/v1/orders/{Id}``."""

    @respx.mock
    async def test_get_order_exposes_equipment_id(self) -> None:
        route = respx.get(f"{BASE}/account/v1/orders/{ORDER_ID}").mock(
            return_value=httpx.Response(200, json=PROVISIONED_ORDER)
        )
        api = _api()
        try:
            order = await api.get_order(ORDER_ID)
        finally:
            await api.aclose()
        assert route.calls.last.request.method == "GET"
        vps_services = order.vps_services()
        assert [service.id for service in vps_services] == ["SVC-1"]
        assert vps_services[0].status == ServiceStatus.ACTIVE
        assert vps_services[0].is_provisioned
        assert vps_services[0].is_vps
        assert isinstance(vps_services[0].price_per_frequency, Decimal)
        assert vps_services[0].price_per_frequency == Decimal("9.99")
        # The delivered VPS is identified by the provider's own equipmentId.
        assert order.first_equipment_id() == "VPS02_1"

    @respx.mock
    async def test_get_order_encodes_hostile_path_parameter(self) -> None:
        route = respx.route(method="GET", url__regex=rf"{BASE}/account/v1/orders/.*").mock(
            return_value=httpx.Response(404, json={"errorMessage": "unknown"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebNotFoundError):
                await api.get_order("LS-ORD-1/../../admin?x=1")
        finally:
            await api.aclose()
        raw = route.calls.last.request.url.raw_path.decode()
        assert raw.endswith("/LS-ORD-1%2F..%2F..%2Fadmin%3Fx%3D1")

    async def test_empty_order_id_is_rejected_before_any_request(self) -> None:
        api = _api()
        try:
            with pytest.raises(LeasewebValidationError):
                await api.get_order("")
        finally:
            await api.aclose()

    @respx.mock
    async def test_cancelled_service_is_reported_as_failed(self) -> None:
        respx.get(f"{BASE}/account/v1/orders/{ORDER_ID}").mock(
            return_value=httpx.Response(
                200,
                json=_order(
                    services=[
                        {
                            "id": "SVC-1",
                            "productId": "VIRTUAL_SERVER",
                            "status": "CANCELLED",
                        }
                    ]
                ),
            )
        )
        api = _api()
        try:
            order = await api.get_order(ORDER_ID)
        finally:
            await api.aclose()
        service = order.vps_services()[0]
        assert service.is_failed
        assert not service.is_provisioning
        assert order.first_equipment_id() is None


class TestReadOnlyGuarantee:
    """Reconciliation must never be able to create or change anything."""

    def test_client_exposes_no_mutating_methods(self) -> None:
        forbidden = ("create", "update", "delete", "cancel", "submit", "post", "put", "patch")
        methods = [
            name
            for name in dir(LeaseWebAccountOrdersApi)
            if not name.startswith("_") and callable(getattr(LeaseWebAccountOrdersApi, name))
        ]
        for name in methods:
            assert not any(token in name.lower() for token in forbidden), name

    @respx.mock
    async def test_every_request_the_client_makes_is_a_get(self) -> None:
        route = respx.route(method="GET", url__regex=rf"{BASE}/account/v1/orders.*").mock(
            return_value=httpx.Response(200, json={"orders": [], "_metadata": _meta(0)})
        )
        api = _api()
        try:
            await api.list_orders()
        finally:
            await api.aclose()
        assert all(call.request.method == "GET" for call in route.calls)


class TestUnknownValueCompatibility:
    """A new provider status/product must not break the reconciliation read."""

    @respx.mock
    async def test_unknown_status_and_product_are_preserved(self) -> None:
        payload = _order(
            type="SOMETHING_NEW",
            origin="PARTNER_API",
            services=[
                {
                    "id": "SVC-9",
                    "productId": "QUANTUM_VPS",
                    "status": "TO_BE_SOMETHING",
                    "pricePerFrequency": "1.05",
                }
            ],
        )
        respx.get(f"{BASE}/account/v1/orders/{ORDER_ID}").mock(
            return_value=httpx.Response(200, json=payload)
        )
        api = _api()
        try:
            order = await api.get_order(ORDER_ID)
        finally:
            await api.aclose()
        assert order.type == "SOMETHING_NEW"
        assert order.origin == "PARTNER_API"
        service = order.services[0]
        assert service.status == "TO_BE_SOMETHING"
        assert service.product_id == "QUANTUM_VPS"
        # Unknown families are simply not VPS lines.
        assert not service.is_vps
        assert order.vps_services() == []

    @respx.mock
    async def test_missing_metadata_still_yields_the_rows(self) -> None:
        respx.get(f"{BASE}/account/v1/orders").mock(
            return_value=httpx.Response(200, json={"orders": [_order()]})
        )
        api = _api()
        try:
            page = await api.list_orders()
        finally:
            await api.aclose()
        assert [order.id for order in page.items] == [ORDER_ID]
        # No ``_metadata`` -> no invented pagination facts.
        assert page.metadata is None


def test_account_order_model_exists_for_typing() -> None:
    """The exported DTO is the typed boundary (never a raw dict)."""
    order = AccountOrder.model_validate(_order())
    assert order.services[0].id == "SVC-1"
