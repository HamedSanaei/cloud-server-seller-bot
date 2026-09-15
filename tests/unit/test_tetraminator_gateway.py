"""Fake-HTTP tests for the Tetraminator gateway.

Covers the documented contract only (create invoice, inquiry, error
table): Toman conversion, minimum enforcement, header presence, success
parsing, failure mapping, and no-secret-leakage with a recognizable fake
key. No test here performs a real payment or credits a wallet.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import GatewayCapability, PaymentStatus, gateway_supports
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.tetraminator.client import (
    MINIMUM_TOMAN,
    TetraminatorGateway,
    toman_price_for,
)

KEY = "tetra_TEST_SUPER_SECRET_API_KEY"
IK = IdempotencyKey("tetraminator-test-key-1")
PAY_ID = "4b84b14d2e90f1bc8123"
LINK = f"https://t.me/tetraminator_bot?start=pay_{PAY_ID}"


def _gateway(handler: Any, **kw: Any) -> TetraminatorGateway:
    gateway = TetraminatorGateway(
        api_key=KEY,
        callback_url="https://example.com/webhooks/payments/tetraminator",
        **kw,
    )
    gateway._client = AsyncMock()  # type: ignore[method-assign]
    gateway._client.post = AsyncMock(side_effect=handler)
    gateway._client.get = AsyncMock(side_effect=handler)
    return gateway


def _response(status_code: int, payload: Any) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("GET", "https://tetraminator.test/"),
    )


def _created() -> dict[str, Any]:
    return {
        "status": True,
        "message": "Invoice created successfully",
        "pay_id": PAY_ID,
        "payment_link": LINK,
    }


class TestMoneyBoundary:
    def test_toman_identity_for_irt(self) -> None:
        assert toman_price_for(50_000, "IRT") == 50_000
        assert toman_price_for(1, "IRT") == 1

    def test_non_irt_refused_without_conversion(self) -> None:
        with pytest.raises(ValueError, match="IRT"):
            toman_price_for(50_000, "EUR")
        with pytest.raises(ValueError, match="IRT"):
            toman_price_for(50_000, "IRR")

    def test_non_positive_rejected(self) -> None:
        with pytest.raises(ValueError):
            toman_price_for(0, "IRT")
        with pytest.raises(ValueError):
            toman_price_for(-5, "IRT")

    def test_minimum_is_documented_value(self) -> None:
        assert MINIMUM_TOMAN == 50_000


class TestCapabilities:
    def test_advertises_create_and_verify_only(self) -> None:
        gateway = TetraminatorGateway(api_key=KEY)
        assert gateway_supports(gateway, GatewayCapability.CREATE_PAYMENT)
        assert gateway_supports(gateway, GatewayCapability.VERIFY_PAYMENT)
        assert not gateway_supports(gateway, GatewayCapability.REFUND)
        assert gateway.key == "tetraminator"
        assert gateway.supported_currency == "IRT"
        assert gateway.minimum_charge_minor == 50_000


class TestCreateInvoice:
    async def test_posts_documented_shape_with_key_header(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            assert url.endswith("/invoice/create")
            assert kw["headers"]["X-API-KEY"] == KEY
            assert kw["headers"]["Content-Type"] == "application/json"
            body = kw["json"]
            assert body["price"] == 500_000
            assert isinstance(body["price"], int)
            assert body["callback_url"] == "https://example.com/webhooks/payments/tetraminator"
            return _response(201, _created())

        intent = await _gateway(handler).create_payment(
            amount_minor=500_000,
            currency="IRT",
            reference="user-1",
            idempotency_key=IK,
            redirect_url="https://example.com/webhooks/payments/tetraminator",
        )
        assert intent.status is PaymentStatus.PENDING
        assert intent.gateway_payment_id == PAY_ID
        assert intent.redirect_url == LINK
        assert intent.amount_minor == 500_000
        assert intent.currency == "IRT"

    async def test_below_minimum_rejected_before_io(self) -> None:
        calls: list[str] = []

        def handler(url: str, **kw: Any) -> httpx.Response:
            calls.append(url)
            return _response(201, _created())

        with pytest.raises(ValueError, match="minimum"):
            await _gateway(handler).create_payment(
                amount_minor=49_999,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )
        assert calls == []

    async def test_exact_minimum_accepted(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            assert kw["json"]["price"] == 50_000
            return _response(201, _created())

        intent = await _gateway(handler).create_payment(
            amount_minor=50_000,
            currency="IRT",
            reference="user-1",
            idempotency_key=IK,
            redirect_url="https://example.com/cb",
        )
        assert intent.gateway_payment_id == PAY_ID

    async def test_status_false_rejected(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(201, {"status": False, "message": "nope"})

        with pytest.raises(ProviderError):
            await _gateway(handler).create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )

    async def test_missing_pay_id_rejected(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            body = dict(_created())
            del body["pay_id"]
            return _response(201, body)

        with pytest.raises(ProviderError, match="pay_id"):
            await _gateway(handler).create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )

    async def test_missing_payment_link_rejected(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            body = dict(_created())
            del body["payment_link"]
            return _response(201, body)

        with pytest.raises(ProviderError, match="payment_link"):
            await _gateway(handler).create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )

    async def test_400_mapped_safely(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(400, {"status": False, "message": "bad data"})

        with pytest.raises(ProviderError) as exc:
            await _gateway(handler).create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )
        assert KEY not in str(exc.value)

    async def test_401_maps_to_auth_error(self) -> None:
        gateway = _gateway(lambda url, **kw: _response(401, {}))
        with pytest.raises(ProviderAuthError):
            await gateway.create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )

    async def test_403_maps_to_disabled_error(self) -> None:
        gateway = _gateway(lambda url, **kw: _response(403, {}))
        with pytest.raises(ProviderError, match="disabled"):
            await gateway.create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )

    async def test_timeout_and_5xx_are_unavailable(self) -> None:
        gateway = _gateway(lambda url, **kw: _response(500, {}))
        with pytest.raises(ProviderUnavailable):
            await gateway.create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )

    async def test_429_is_rate_limited(self) -> None:
        gateway = _gateway(lambda url, **kw: _response(429, {}))
        with pytest.raises(ProviderRateLimited):
            await gateway.create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )

    async def test_non_json_rejected_safely(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return httpx.Response(
                201,
                content=b"not json",
                headers={"Content-Type": "application/json"},
                request=httpx.Request("POST", "https://tetraminator.test/"),
            )

        with pytest.raises(ProviderError) as exc:
            await _gateway(handler).create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="https://example.com/cb",
            )
        assert KEY not in str(exc.value)

    async def test_callback_url_validated(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(201, _created())

        with pytest.raises(ValueError, match="http"):
            await _gateway(handler).create_payment(
                amount_minor=60_000,
                currency="IRT",
                reference="user-1",
                idempotency_key=IK,
                redirect_url="ftp://example.com/cb",
            )

    async def test_key_never_leaks(
        self, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import logging

        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(403, {"status": False})

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(ProviderError):
                await _gateway(handler).create_payment(
                    amount_minor=60_000,
                    currency="IRT",
                    reference="user-1",
                    idempotency_key=IK,
                    redirect_url="https://example.com/cb",
                )
        assert KEY not in caplog.text
        assert KEY not in capsys.readouterr().out
        gateway = TetraminatorGateway(api_key=KEY)
        assert KEY not in repr(gateway)
        await gateway.close()


class TestInquiry:
    def _paid(self) -> dict[str, Any]:
        return {"status": True, "payment_status": "paid", "pay_id": PAY_ID, "amount": 500000}

    async def test_inquiry_sends_documented_get_with_key(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            assert url.endswith(f"/payment/inquiry/{PAY_ID}")
            assert kw["headers"]["X-API-KEY"] == KEY
            return _response(200, self._paid())

        intent = await _gateway(handler).verify_payment(PAY_ID)
        assert intent.status is PaymentStatus.SUCCEEDED
        assert intent.gateway_payment_id == PAY_ID
        assert intent.amount_minor == 500000
        assert intent.currency == "IRT"

    async def test_unpaid_response_stays_pending(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(200, {"status": True, "payment_status": "pending", "pay_id": PAY_ID})

        intent = await _gateway(handler).verify_payment(PAY_ID)
        assert intent.status is PaymentStatus.PENDING

    async def test_status_false_stays_pending(self) -> None:
        def handler(url: str, **kw: Any) -> httpx.Response:
            return _response(200, {"status": False, "payment_status": "paid", "pay_id": PAY_ID})

        intent = await _gateway(handler).verify_payment(PAY_ID)
        assert intent.status is PaymentStatus.PENDING

    async def test_empty_pay_id_rejected(self) -> None:
        with pytest.raises(ValueError):
            await _gateway(lambda url, **kw: _response(200, {})).verify_payment("  ")

    async def test_inquiry_401_is_auth_error(self) -> None:
        with pytest.raises(ProviderAuthError):
            await _gateway(lambda url, **kw: _response(401, {})).verify_payment(PAY_ID)

    async def test_inquiry_timeout_is_transient(self) -> None:
        async def handler(url: str, **kw: Any) -> httpx.Response:
            raise httpx.TimeoutException("slow", request=httpx.Request("GET", url))

        with pytest.raises(ProviderUnavailable):
            await _gateway(handler).verify_payment(PAY_ID)
