"""Mocked contract tests for the Leaseweb VPS **Ordering** API (3 operations).

``GET  /ordering/v1/products/vps``
``GET  /ordering/v1/products/vps/{vpsId}``
``POST /ordering/v1/products/vps/{vpsId}/order``  <-- BILLABLE

Every test mocks the HTTP boundary with ``respx``: NO test can reach
Leaseweb and NO test can create a real contract. The billable order tests
prove the safety properties the platform depends on:

- the documented body is serialized with camelCase aliases and the exact
  option values;
- a read timeout after transmission, a 5xx and a 2xx without ``orderId``
  are all AMBIGUOUS and are never retried (a single local operation can
  therefore never buy two VPSes);
- a definitive 4xx rejection is NOT ambiguous;
- money is parsed as :class:`decimal.Decimal`, never as a float.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from cloud_platform.providers.leaseweb.errors import (
    LeasewebAmbiguousMutationError,
    LeasewebAuthenticationError,
    LeasewebNotFoundError,
    LeasewebServerError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.ordering_api import (
    ContractTerm,
    LeaseWebOrderingApi,
    OrderVpsRequest,
    ServiceLevelAgreement,
)
from cloud_platform.providers.leaseweb.transport import LeasewebTransport, Throttle

KEY = "test-leaseweb-key"
BASE = "https://api.test"
PRODUCT = "VPS02_1"


def _no_sleep() -> Throttle:
    async def wait(_seconds: float) -> None:
        return None

    return Throttle(max_rps=100_000.0, wait=wait)


def _api() -> LeaseWebOrderingApi:
    return LeaseWebOrderingApi(LeasewebTransport(KEY, BASE, throttle=_no_sleep(), max_retries=2))


def _meta(total: int, offset: int = 0, limit: int = 100) -> dict[str, int]:
    return {"totalCount": total, "offset": offset, "limit": limit}


PRODUCT_LIST_ITEM = {
    "id": PRODUCT,
    "name": "VPS 2.1",
    "vCpu": "2",
    "vRam": "4",
    "nvmeStorage": "100 GB",
    "traffic": "20 TB",
    "price": {"currency": "EUR", "basePrice": "9.99", "discount": "0.00", "total": "9.99"},
}

PRODUCT_DETAIL = {
    "id": PRODUCT,
    "name": "VPS 2.1",
    "vCpu": "2",
    "vRam": "4",
    "nvmeStorage": "100 GB",
    "traffic": "20 TB",
    "location": ["AMS-01", "FRA-01"],
    "price": {
        "currency": "EUR",
        "basePrice": "9.99",
        "tax": "0.00",
        "setupFee": "0.00",
        "fee": "0.00",
        "total": "9.99",
        "contractTerms": [
            {"key": "1_MONTH", "total": "9.99"},
            {"key": "12_MONTHS", "total": "99.99"},
        ],
        "billingCycles": [{"key": "1_MONTH", "total": "9.99"}],
        "details": {"cpu": {"type": "shared"}},
    },
    "configurationOptions": {
        "diskUpgrade": [{"name": "250 GB", "selected": False, "price": "5.00", "currency": "EUR"}],
        "operatingSystem": [
            {"name": "Ubuntu 22.04", "selected": True, "price": "0.00", "currency": "EUR"},
            {
                "name": "Windows 2022",
                "selected": False,
                "price": "14.00",
                "currency": "EUR",
            },
        ],
        "controlPanel": [{"name": "Plesk", "selected": False, "price": "7.50", "currency": "EUR"}],
        "serviceLevelAgreement": [
            {"name": "Basic", "selected": True, "price": "0.00", "currency": "EUR"}
        ],
    },
}


class TestProductCatalogue:
    """``GET /ordering/v1/products/vps`` + its documented filters."""

    @respx.mock
    async def test_list_products_sends_documented_query(self) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(
                200, json={"vpss": [PRODUCT_LIST_ITEM], "_metadata": _meta(1)}
            )
        )
        api = _api()
        try:
            page = await api.list_products(location="AMS-01", limit=10, offset=20)
        finally:
            await api.aclose()
        assert dict(route.calls.last.request.url.params) == {
            "location": "AMS-01",
            "limit": "10",
            "offset": "20",
        }
        assert page.metadata is not None and page.metadata.total_count == 1
        item = page.items[0]
        assert item.id == PRODUCT
        assert item.vcpu_count == 2
        assert item.ram_gb == 4
        assert item.disk_gb == 100
        assert item.price is not None
        assert isinstance(item.price.total, Decimal)
        assert item.price.total == Decimal("9.99")

    @respx.mock
    async def test_list_products_without_filters_sends_no_query(self) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(200, json={"vpss": [], "_metadata": _meta(0)})
        )
        api = _api()
        try:
            page = await api.list_products()
        finally:
            await api.aclose()
        assert not route.calls.last.request.url.query
        assert page.items == []

    @respx.mock
    async def test_all_products_follows_provider_pagination(self) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "vpss": [PRODUCT_LIST_ITEM],
                        "_metadata": _meta(2, offset=0, limit=1),
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "vpss": [{**PRODUCT_LIST_ITEM, "id": "VPS04_1"}],
                        "_metadata": _meta(2, offset=1, limit=1),
                    },
                ),
            ]
        )
        api = _api()
        try:
            products = await api.all_products(location="AMS-01", page_size=1)
        finally:
            await api.aclose()
        assert [product.id for product in products] == [PRODUCT, "VPS04_1"]
        assert route.call_count == 2
        offsets = [call.request.url.params["offset"] for call in route.calls]
        assert offsets == ["0", "1"]


class TestProductConfiguration:
    """``GET /ordering/v1/products/vps/{vpsId}`` with every documented parameter."""

    @respx.mock
    async def test_get_product_sends_every_documented_parameter(self) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps/{PRODUCT}").mock(
            return_value=httpx.Response(200, json={"vps": PRODUCT_DETAIL})
        )
        api = _api()
        try:
            detail = await api.get_product(
                PRODUCT,
                location="AMS-01",
                disk_upgrade="250 GB",
                operating_system="Ubuntu 22.04",
                control_panel="Plesk",
                contract_term=ContractTerm.ONE_MONTH,
                billing_cycle="1_MONTH",
                service_level_agreement=ServiceLevelAgreement.BASIC,
            )
        finally:
            await api.aclose()
        assert dict(route.calls.last.request.url.params) == {
            "location": "AMS-01",
            "diskUpgrade": "250 GB",
            "operatingSystem": "Ubuntu 22.04",
            "controlPanel": "Plesk",
            "contractTerm": "1_MONTH",
            "billingCycle": "1_MONTH",
            "serviceLevelAgreement": "Basic",
        }
        assert detail.id == PRODUCT
        assert detail.available_in("FRA-01")
        assert not detail.available_in("SFO-01")
        price = detail.price
        assert price is not None
        assert price.total_for_term("12_MONTHS") == Decimal("99.99")
        assert price.total_for_cycle("1_MONTH") == Decimal("9.99")
        assert price.details["cpu"]["type"] == "shared"
        options = detail.configuration_options
        assert options is not None
        assert [option.name for option in options.free_operating_systems()] == ["Ubuntu 22.04"]
        # The documented camelCase group name and the Python attribute name
        # are interchangeable.
        assert options.find("controlPanel", "Plesk") is not None
        assert options.find("control_panel", "Plesk") is not None
        assert options.find("controlPanel", "cPanel") is None

    @respx.mock
    async def test_get_product_location_is_the_only_required_parameter(self) -> None:
        route = respx.get(f"{BASE}/ordering/v1/products/vps/{PRODUCT}").mock(
            return_value=httpx.Response(200, json=PRODUCT_DETAIL)
        )
        api = _api()
        try:
            await api.get_product(PRODUCT, location="AMS-01")
        finally:
            await api.aclose()
        assert dict(route.calls.last.request.url.params) == {"location": "AMS-01"}

    @respx.mock
    async def test_product_path_parameter_is_percent_encoded(self) -> None:
        route = respx.get(url__regex=rf"{BASE}/ordering/v1/products/vps/.*").mock(
            return_value=httpx.Response(200, json=PRODUCT_DETAIL)
        )
        api = _api()
        try:
            await api.get_product("VPS02_1/../../admin?x=1", location="AMS-01")
        finally:
            await api.aclose()
        request = route.calls.last.request
        raw = request.url.raw_path.decode().split("?")[0]
        assert raw.endswith("/VPS02_1%2F..%2F..%2Fadmin%3Fx%3D1")
        assert dict(request.url.params) == {"location": "AMS-01"}

    async def test_empty_product_id_is_rejected_before_any_request(self) -> None:
        api = _api()
        try:
            with pytest.raises(LeasewebValidationError):
                await api.get_product("   ", location="AMS-01")
        finally:
            await api.aclose()


class TestOrdering:
    @respx.mock
    async def test_order_posts_documented_body_and_returns_order_id(self) -> None:
        route = respx.post(f"{BASE}/ordering/v1/products/vps/{PRODUCT}/order").mock(
            return_value=httpx.Response(201, json={"orderId": 12345})
        )
        api = _api()
        try:
            result = await api.order_vps(
                PRODUCT,
                OrderVpsRequest(
                    location="AMS-01",
                    disk_upgrade="250 GB",
                    operating_system="Ubuntu 22.04",
                    control_panel="Plesk",
                    service_level_agreement=ServiceLevelAgreement.BASIC,
                    contract_term=ContractTerm.ONE_MONTH,
                    billing_cycle="1_MONTH",
                ),
            )
        finally:
            await api.aclose()
        request = route.calls.last.request
        assert request.method == "POST"
        assert request.headers["X-LSW-Auth"] == KEY
        assert json_body(request) == {
            "location": "AMS-01",
            "diskUpgrade": "250 GB",
            "operatingSystem": "Ubuntu 22.04",
            "controlPanel": "Plesk",
            "serviceLevelAgreement": "Basic",
            "contractTerm": "1_MONTH",
            "billingCycle": "1_MONTH",
        }
        assert result.order_id == 12345
        assert result.order_id_str == "12345"

    @respx.mock
    async def test_order_omits_unset_options(self) -> None:
        route = respx.post(f"{BASE}/ordering/v1/products/vps/{PRODUCT}/order").mock(
            return_value=httpx.Response(201, json={"orderId": 1})
        )
        api = _api()
        try:
            await api.order_vps(PRODUCT, OrderVpsRequest(location="AMS-01"))
        finally:
            await api.aclose()
        assert json_body(route.calls.last.request) == {"location": "AMS-01"}

    @respx.mock
    async def test_read_timeout_after_transmission_is_ambiguous_and_never_resent(self) -> None:
        route = respx.post(f"{BASE}/ordering/v1/products/vps/{PRODUCT}/order").mock(
            side_effect=httpx.ReadTimeout("no response")
        )
        api = _api()
        try:
            with pytest.raises(LeasewebAmbiguousMutationError):
                await api.order_vps(PRODUCT, OrderVpsRequest(location="AMS-01"))
        finally:
            await api.aclose()
        assert route.call_count == 1

    @respx.mock
    async def test_5xx_is_ambiguous_and_never_resent(self) -> None:
        route = respx.post(f"{BASE}/ordering/v1/products/vps/{PRODUCT}/order").mock(
            return_value=httpx.Response(503, json={"errorMessage": "down"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebAmbiguousMutationError):
                await api.order_vps(PRODUCT, OrderVpsRequest(location="AMS-01"))
        finally:
            await api.aclose()
        assert route.call_count == 1

    @respx.mock
    async def test_2xx_without_order_id_is_ambiguous(self) -> None:
        route = respx.post(f"{BASE}/ordering/v1/products/vps/{PRODUCT}/order").mock(
            return_value=httpx.Response(201, json={})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebAmbiguousMutationError):
                await api.order_vps(PRODUCT, OrderVpsRequest(location="AMS-01"))
        finally:
            await api.aclose()
        assert route.call_count == 1

    @respx.mock
    async def test_definitive_rejection_is_not_ambiguous(self) -> None:
        route = respx.post(f"{BASE}/ordering/v1/products/vps/{PRODUCT}/order").mock(
            return_value=httpx.Response(401, json={"errorMessage": "bad key"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebAuthenticationError):
                await api.order_vps(PRODUCT, OrderVpsRequest(location="AMS-01"))
        finally:
            await api.aclose()
        assert route.call_count == 1

    @respx.mock
    async def test_server_error_on_a_read_is_not_ambiguous(self) -> None:
        respx.get(f"{BASE}/ordering/v1/products/vps").mock(
            return_value=httpx.Response(500, json={"errorMessage": "boom"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebServerError):
                await api.list_products()
        finally:
            await api.aclose()

    @respx.mock
    async def test_missing_product_maps_to_not_found(self) -> None:
        respx.get(f"{BASE}/ordering/v1/products/vps/{PRODUCT}").mock(
            return_value=httpx.Response(404, json={"errorMessage": "unknown"})
        )
        api = _api()
        try:
            with pytest.raises(LeasewebNotFoundError):
                await api.get_product(PRODUCT, location="AMS-01")
        finally:
            await api.aclose()


class TestMoneyParsing:
    """Prices are Decimals: binary float must never touch billing."""

    @respx.mock
    async def test_decimal_precision_is_preserved_exactly(self) -> None:
        payload = {
            **PRODUCT_DETAIL,
            "price": {
                **PRODUCT_DETAIL["price"],
                "basePrice": "0.1",
                "total": "0.3",
            },
        }
        respx.get(f"{BASE}/ordering/v1/products/vps/{PRODUCT}").mock(
            return_value=httpx.Response(200, json=payload)
        )
        api = _api()
        try:
            detail = await api.get_product(PRODUCT, location="AMS-01")
        finally:
            await api.aclose()
        assert detail.price is not None
        assert repr(detail.price.base_price) == "Decimal('0.1')"
        # The classic float trap: 0.1 + 0.2 != 0.3. Decimals are exact.
        assert detail.price.base_price + detail.price.base_price * 2 == detail.price.total


class TestUnknownValueCompatibility:
    """New provider option values are preserved instead of breaking a read."""

    @respx.mock
    async def test_unknown_contract_term_and_sla_are_preserved(self) -> None:
        payload = {
            **PRODUCT_DETAIL,
            "price": {**PRODUCT_DETAIL["price"], "contractTerm": "60_MONTHS"},
            "configurationOptions": {
                **PRODUCT_DETAIL["configurationOptions"],
                "serviceLevelAgreement": [
                    {"name": "Diamond", "selected": False, "price": "99.00", "currency": "EUR"}
                ],
            },
        }
        respx.get(f"{BASE}/ordering/v1/products/vps/{PRODUCT}").mock(
            return_value=httpx.Response(200, json=payload)
        )
        api = _api()
        try:
            detail = await api.get_product(PRODUCT, location="AMS-01")
        finally:
            await api.aclose()
        assert detail.price is not None and detail.price.contract_term == "60_MONTHS"
        options = detail.configuration_options
        assert options is not None
        assert options.service_level_agreement[0].name == "Diamond"


def json_body(request: httpx.Request) -> dict[str, object]:
    """The decoded JSON request body."""
    import json

    return json.loads(request.content.decode())
