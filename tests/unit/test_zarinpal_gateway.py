"""Fake-HTTP tests for the ZarinPal gateway (M09-004).

Covers the create/verify round-trip, the failure table, currency gating,
sandbox StartPay URLs and no-secret-leakage. Live sandbox evidence is
recorded in docs/payments/ZARINPAL.md by running the same flow with
ZARINPAL_SANDBOX=true.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import GatewayCapability, PaymentStatus, gateway_supports
from cloud_platform.providers.errors import ProviderAuthError, ProviderError
from cloud_platform.providers.zarinpal.client import ZarinPalGateway

MERCHANT = "test-merchant-id-DO-NOT-LEAK"
IK = IdempotencyKey("zarinpal-test-key-1")


def _gateway(handler: Any, sandbox: bool = False) -> ZarinPalGateway:
    gateway = ZarinPalGateway(
        merchant_id=MERCHANT, sandbox=sandbox, callback_url="https://example.com/cb"
    )
    gateway._client = AsyncMock()  # type: ignore[method-assign]
    gateway._client.post = AsyncMock(side_effect=handler)
    return gateway


def _response(status_code: int, payload: Any) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", "https://zarinpal.test/request.json"),
    )


class TestCapabilities:
    def test_advertises_create_and_verify_only(self) -> None:
        gateway = ZarinPalGateway(merchant_id=MERCHANT, callback_url="https://example.com/cb")
        assert gateway_supports(gateway, GatewayCapability.CREATE_PAYMENT)
        assert gateway_supports(gateway, GatewayCapability.VERIFY_PAYMENT)
        assert not gateway_supports(gateway, GatewayCapability.REFUND)

    async def test_refund_is_unsupported(self) -> None:
        from cloud_platform.providers.base import UnsupportedGatewayOperation

        gateway = _gateway(lambda url, **kw: _response(200, {}))
        with pytest.raises(UnsupportedGatewayOperation):
            await gateway.refund(gateway_payment_id="a", amount_minor=100, idempotency_key=IK)


class TestCreatePayment:
    async def test_create_returns_pending_intent_with_startpay_url(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            assert url.endswith("/request.json")
            body = kw["json"]
            assert body["merchant_id"] == MERCHANT
            assert body["amount"] == 50000
            assert body["callback_url"] == "https://example.com/cb"
            return _response(
                200, {"data": {"code": 100, "authority": "A0001", "message": "OK"}, "errors": {}}
            )

        intent = await _gateway(handler).create_payment(
            amount_minor=50000, currency="IRR", reference="order-1", idempotency_key=IK
        )
        assert intent.status is PaymentStatus.PENDING
        assert intent.gateway_payment_id == "A0001"
        assert intent.redirect_url == "https://www.zarinpal.com/pg/StartPay/A0001"

    async def test_sandbox_startpay_url(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(200, {"data": {"code": 100, "authority": "S1"}, "errors": {}})

        intent = await _gateway(handler, sandbox=True).create_payment(
            amount_minor=1000, currency="IRR", reference="r", idempotency_key=IK
        )
        assert intent.redirect_url == "https://sandbox.zarinpal.com/pg/StartPay/S1"

    async def test_non_irr_rejected(self) -> None:
        gateway = _gateway(lambda url, **kw: _response(200, {}))
        with pytest.raises(ValueError, match="IRR"):
            await gateway.create_payment(
                amount_minor=100, currency="EUR", reference="r", idempotency_key=IK
            )

    async def test_failed_code_raises_without_leaking_merchant(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(200, {"data": {"code": -9, "message": "validation"}, "errors": {}})

        with pytest.raises(ProviderError) as exc:
            await _gateway(handler).create_payment(
                amount_minor=1000, currency="IRR", reference="r", idempotency_key=IK
            )
        assert MERCHANT not in str(exc.value)

    async def test_401_maps_to_auth_error(self) -> None:
        gateway = _gateway(lambda url, **kw: _response(401, {}))
        with pytest.raises(ProviderAuthError):
            await gateway.create_payment(
                amount_minor=1000, currency="IRR", reference="r", idempotency_key=IK
            )


class TestVerify:
    async def test_verify_with_amount_success(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            assert url.endswith("/verify.json")
            return _response(200, {"data": {"code": 100, "ref_id": 12345}, "errors": {}})

        intent = await _gateway(handler).verify_with_amount("A0001", 50000)
        assert intent.status is PaymentStatus.SUCCEEDED
        assert intent.metadata["ref_id"] == "12345"

    async def test_verify_failed_authority(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(200, {"data": {"code": -54, "message": "archived"}, "errors": {}})

        intent = await _gateway(handler).verify_with_amount("BAD", 50000)
        assert intent.status is PaymentStatus.FAILED
