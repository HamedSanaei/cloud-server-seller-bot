"""Fake-HTTP tests for the Leaseweb ordering-VPS adapter (LEASEWEB-MVP).

Covers the Ordering API contract: X-LSW-Auth header, product list/detail
parsing, price parsing (integer minor units, no float), order request
serialization, get-before-create dedup, error mapping and rate-limit
retry. POSTs are always mocked — no real order can ever be placed here.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import CreateServerRequest
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.client import Throttle
from cloud_platform.providers.leaseweb.ordering import (
    LeaseWebOrderingProvider,
    to_minor_units,
)

KEY = "LSW-ORDERING-TEST-KEY"
IK = IdempotencyKey("order-create:test-server")


def _no_sleep() -> Throttle:
    async def wait(_s: float) -> None:
        return None

    return Throttle(max_rps=1000.0, wait=wait)


def _response(status_code: int, payload: Any = None) -> httpx.Response:
    body = b"" if payload is None else json.dumps(payload).encode()
    return httpx.Response(
        status_code,
        headers={"Content-Type": "application/json"},
        content=body,
        request=httpx.Request("GET", "https://api.test/x"),
    )


def _provider(handler: Any) -> LeaseWebOrderingProvider:
    provider = LeaseWebOrderingProvider(
        api_key=KEY,
        base_url="https://api.test",
        locations=("AMS-01", "FRA-01"),
        throttle=_no_sleep(),
        max_retries=2,
    )
    provider._client = AsyncMock()  # type: ignore[method-assign]
    provider._client.request = AsyncMock(side_effect=handler)
    return provider


def _product_item(
    item_id: str = "VPS02_1",
    *,
    name: str = "VPS S",
    vcpu: str = "2",
    ram: str = "4",
    disk: str = "100 GB",
    total: str = "12.99",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "vCpu": vcpu,
        "vRam": ram,
        "nvmeStorage": disk,
        "traffic": "10 TB",
        "price": {"basePrice": "10.00", "total": total, "currency": "EUR"},
    }


def _product_detail_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "vps": {
            **item,
            "location": ["AMS-01", "FRA-01"],
            "configurationOptions": {
                "operatingSystem": {
                    "options": [
                        {"name": "Ubuntu 24.04", "price": "0.00", "currency": "EUR"},
                        {"name": "Windows Server 2022", "price": "15.00", "currency": "EUR"},
                    ]
                },
                "controlPanel": {"options": []},
            },
            "price": {
                "basePrice": "10.00",
                "total": "12.99",
                "currency": "EUR",
                "contractTerms": [
                    {"key": "1_MONTH", "total": "12.99"},
                    {"key": "12_MONTHS", "total": "11.49"},
                ],
            },
        }
    }


class TestAuthHeader:
    def test_constructor_sets_lsw_auth_header(self) -> None:
        provider = LeaseWebOrderingProvider(
            api_key=KEY, base_url="https://x", locations=("AMS-01",), throttle=_no_sleep()
        )
        headers = {k.lower(): v for k, v in provider._client.headers.items()}
        assert headers["x-lsw-auth"] == KEY
        assert "authorization" not in headers

    async def test_key_never_leaks_into_errors(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(401, {"errorMessage": "Invalid API key"}))
        with pytest.raises(ProviderAuthError) as exc:
            await provider.list_products("AMS-01")
        assert KEY not in str(exc.value)


class TestCatalogParsing:
    async def test_list_products_parses_specs_and_price(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(
                200, {"_metadata": {"totalCount": 1}, "vpss": [_product_item()]}
            )
        )
        products = await provider.list_products("AMS-01")
        assert len(products) == 1
        product = products[0]
        assert product.id == "VPS02_1"
        assert product.vcpu == 2
        assert product.ram_gb == 4  # vRam is GB per the ordering API
        assert product.disk_gb == 100
        assert product.traffic == "10 TB"
        assert product.monthly_price_minor == 1299
        assert product.currency == "EUR"

    async def test_list_products_paginates(self) -> None:
        calls = 0

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            nonlocal calls
            calls += 1
            offset = int((kw.get("params") or {}).get("offset", 0))
            items = [_product_item(item_id=f"VPS{i}") for i in range(offset, offset + 2)]
            return _response(200, {"_metadata": {"totalCount": 4}, "vpss": items})

        provider = _provider(handler)
        products = await provider.list_products("AMS-01")
        assert len(products) == 4
        assert calls == 2

    async def test_get_product_parses_options_and_monthly_price(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(200, _product_detail_payload(_product_item()))
        )
        detail = await provider.get_product("AMS-01", "VPS02_1")
        assert detail.product.monthly_price_minor == 1299
        names = [o.name for o in detail.os_options]
        assert names == ["Ubuntu 24.04", "Windows Server 2022"]
        assert [o.name for o in detail.free_os_options()] == ["Ubuntu 24.04"]
        assert detail.contract_terms == {"1_MONTH": 1299, "12_MONTHS": 1149}

    async def test_get_product_respects_os_allowlist(self) -> None:
        provider = LeaseWebOrderingProvider(
            api_key=KEY,
            base_url="https://api.test",
            locations=("AMS-01",),
            os_allowlist=("ubuntu 24.04",),
            throttle=_no_sleep(),
        )
        provider._client = AsyncMock()  # type: ignore[method-assign]
        provider._client.request = AsyncMock(
            side_effect=lambda m, p, **kw: _response(200, _product_detail_payload(_product_item()))
        )
        detail = await provider.get_product("AMS-01", "VPS02_1")
        assert [o.name for o in detail.os_options] == ["Ubuntu 24.04"]

    async def test_os_name_allowed_rejects_paid_options_by_default(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(200, _product_detail_payload(_product_item()))
        )
        detail = await provider.get_product("AMS-01", "VPS02_1")
        assert provider.os_name_allowed(detail, "Ubuntu 24.04") is True
        assert provider.os_name_allowed(detail, "Windows Server 2022") is False
        assert provider.os_name_allowed(detail, "Debian 12") is False


class TestPricing:
    def test_to_minor_units_never_uses_float(self) -> None:
        from decimal import Decimal

        assert to_minor_units(Decimal("12.99")) == 1299
        assert to_minor_units(Decimal("0.005")) == 1  # half-up rounding
        assert to_minor_units(Decimal("10")) == 1000


class TestOrdering:
    async def test_order_request_serialization(self) -> None:
        seen: dict[str, Any] = {}

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            seen.update(method=m, path=p, body=kw.get("json"))
            return _response(201, {"orderId": "LS-ORD-123"})

        provider = _provider(handler)
        request = CreateServerRequest(
            name="srv-test",
            plan_id="VPS02_1",
            image_id="Ubuntu 24.04",
            location_id="AMS-01",
        )
        ticket = await provider.place_order(request, IK)
        assert seen["method"] == "POST"
        assert seen["path"] == "/ordering/v1/products/vps/VPS02_1/order"
        assert seen["body"] == {
            "location": "AMS-01",
            "operatingSystem": "Ubuntu 24.04",
            "contractTerm": "1_MONTH",
            "billingCycle": "1_MONTH",
        }
        assert ticket.provider_order_id == "LS-ORD-123"
        assert ticket.state == "accepted"

    async def test_order_deduplicated_by_get_before_create(self) -> None:
        """A retry after a timeout must NOT place a second order."""
        calls: list[str] = []
        from datetime import UTC, datetime

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            calls.append(f"{m} {p}")
            if m == "GET" and p == "/account/v1/orders":
                return _response(
                    200,
                    {
                        "orders": [
                            {
                                "id": "LS-ORD-OLD",
                                "type": "NEW_ORDER",
                                "createdAt": datetime.now(UTC).isoformat(),
                                "services": [
                                    {
                                        "productId": "VIRTUAL_SERVER",
                                        "pricePerFrequency": "12.99",
                                    }
                                ],
                            }
                        ]
                    },
                )
            if m == "POST":
                return _response(201, {"orderId": "LS-ORD-NEW"})
            return _response(200, {"vpss": []})

        provider = _provider(handler)
        request = CreateServerRequest(
            name="srv-test",
            plan_id="VPS02_1",
            image_id="Ubuntu 24.04",
            location_id="AMS-01",
            labels={"price_minor": "1299"},
        )
        ticket = await provider.place_order(request, IK)
        assert ticket.provider_order_id == "LS-ORD-OLD"
        assert ticket.state == "provisioning"
        assert "POST /ordering/v1/products/vps/VPS02_1/order" not in calls

    async def test_order_missing_order_id_is_an_error(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(201, {"status": "ok"}))
        request = CreateServerRequest(
            name="srv", plan_id="VPS02_1", image_id="Ubuntu", location_id="AMS-01"
        )
        from cloud_platform.providers.errors import ProviderError

        with pytest.raises(ProviderError):
            await provider.place_order(request, IK)


class TestErrorMapping:
    async def test_401_maps_to_auth_error(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(401, {"errorMessage": "bad key"}))
        with pytest.raises(ProviderAuthError):
            await provider.list_products("AMS-01")

    async def test_404_maps_to_not_found(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(404, {"errorMessage": "gone"}))
        with pytest.raises(ProviderNotFound):
            await provider.get_product("AMS-01", "VPS02_1")

    async def test_429_maps_to_rate_limited(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(429, {"errorMessage": "slow down"}),
        )
        with pytest.raises(ProviderRateLimited):
            await provider.list_products("AMS-01")

    async def test_500_maps_to_unavailable(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(503, {"errorMessage": "down"}))
        with pytest.raises(ProviderUnavailable):
            await provider.list_products("AMS-01")

    async def test_rate_limit_retry_succeeds_after_wait(self) -> None:
        attempts = 0

        def handler(m: str, p: str, **kw: Any) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _response(429, {"errorMessage": "throttled"})
            return _response(200, {"_metadata": {"totalCount": 0}, "vpss": []})

        waits: list[float] = []

        async def record_wait(seconds: float) -> None:
            waits.append(seconds)

        provider = LeaseWebOrderingProvider(
            api_key=KEY,
            base_url="https://api.test",
            locations=("AMS-01",),
            throttle=Throttle(max_rps=1000.0, wait=record_wait),
            max_retries=2,
        )
        provider._client = AsyncMock()  # type: ignore[method-assign]
        provider._client.request = AsyncMock(side_effect=handler)
        await provider.list_products("AMS-01")
        assert attempts == 2
        # The throttle also records its acquire delays; the retry backoff
        # (0.5s for the first retry) is among them.
        assert 0.5 in waits
        assert len(waits) == 2  # retry backoff + second acquire

    async def test_transport_error_maps_to_unavailable(self) -> None:
        provider = _provider(
            lambda m, p, **kw: (_ for _ in ()).throw(httpx.TimeoutException("timed out"))
        )
        with pytest.raises(ProviderUnavailable):
            await provider.list_products("AMS-01")


class TestVpsManagement:
    async def test_get_server_parses_ips(self) -> None:
        provider = _provider(
            lambda m, p, **kw: _response(
                200,
                {
                    "id": "vps-1",
                    "reference": "srv-1",
                    "state": "running",
                    "datacenter": "AMS-01",
                    "ips": [
                        {"ip": "1.2.3.4", "version": "4"},
                        {"ip": "2001:db8::1", "version": "6"},
                    ],
                },
            )
        )
        server = await provider.get_server("vps-1")
        assert server is not None
        assert server.id == "vps-1"
        assert server.ipv4 == "1.2.3.4"
        assert server.ipv6 == "2001:db8::1"

    async def test_get_server_404_returns_none(self) -> None:
        provider = _provider(lambda m, p, **kw: _response(404, {"errorMessage": "gone"}))
        assert await provider.get_server("vps-missing") is None
