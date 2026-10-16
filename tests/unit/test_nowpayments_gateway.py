"""Hermetic NOWPayments hosted-invoice gateway tests (create + verify only).

All HTTP is served by ``httpx.MockTransport``; no network, credentials or live
provider is ever touched. The fixtures mirror the documented NOWPayments
payloads: ``POST /invoice`` returns an *invoice* id (never a concrete payment
id), and recovery scans ``GET /payment/`` (x-api-key + Bearer JWT) before
confirming a concrete payment through ``GET /payment/{payment_id}``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.payments.domain import PaymentSession
from cloud_platform.modules.payments.inquiry import verify_payment_session
from cloud_platform.providers.base import (
    GatewayCapability,
    PaymentStatus,
    UnsupportedGatewayOperation,
)
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.nowpayments import NowPaymentsGateway
from cloud_platform.providers.nowpayments.client import (
    GATEWAY_KEY,
    ORDER_ID_NAMESPACE,
    PRICE_CURRENCY,
    SUPPORTED_CURRENCY,
)

API_KEY = "np-fixture-api-key-0001"
EMAIL = "merchant@nowpayments.test"
PASSWORD = "np-fixture-secret-0001"
CALLBACK = "https://seller.nowpayments.test/payments/ipn"
MERCHANT_REF = "wallet-nowpayments-attempt-001"
ORDER_REF = "cloud-server-seller:wallet-nowpayments-attempt-001"
INVOICE_ID = "123456789"
PAYMENT_ID = "987654321"
INVOICE_URL = "https://nowpayments.io/payment/?iid=123456789"

INVOICE_RESPONSE: dict[str, Any] = {
    "id": INVOICE_ID,
    "order_id": ORDER_REF,
    "order_description": None,
    "price_amount": "10.5",
    "price_currency": "usd",
    "pay_currency": None,
    "ipn_callback_url": CALLBACK,
    "invoice_url": INVOICE_URL,
    "created_at": "2026-08-05T21:58:56.329Z",
}

PAYMENT_RESPONSE: dict[str, Any] = {
    "payment_id": PAYMENT_ID,
    "invoice_id": int(INVOICE_ID),
    "payment_status": "finished",
    "price_amount": 10.5,
    "price_currency": "usd",
    "order_id": ORDER_REF,
    "pay_currency": "btc",
    "pay_amount": 0.00012345,
    "actually_paid": 0.00012345,
}

OTHER_ORDER_ENTRY: dict[str, Any] = {
    **PAYMENT_RESPONSE,
    "order_id": "cloud-server-seller:another-attempt",
    "payment_id": "987654322",
}
SECOND_MATCH_ENTRY: dict[str, Any] = {
    **PAYMENT_RESPONSE,
    "payment_id": "987654323",
}
FORGED_ORDER_ENTRY: dict[str, Any] = {
    **PAYMENT_RESPONSE,
    "order_id": "some-other-namespace:wallet-nowpayments-attempt-001",
    "payment_id": "987654324",
}
NULL_ORDER_ENTRY: dict[str, Any] = {
    **PAYMENT_RESPONSE,
    "order_id": None,
    "payment_id": "987654325",
}
NO_MATCH_A: dict[str, Any] = {
    **PAYMENT_RESPONSE,
    "order_id": "cloud-server-seller:someone-else",
    "payment_id": "987654326",
}
NO_MATCH_B: dict[str, Any] = {
    **PAYMENT_RESPONSE,
    "order_id": "cloud-server-seller:someone-else",
    "payment_id": "987654327",
}

Handler = Callable[[httpx.Request], httpx.Response]


def page_of(
    entries: list[dict[str, Any]], *, page: int = 0, pages: int = 1, limit: int = 100
) -> dict[str, Any]:
    return {
        "data": entries,
        "total": len(entries),
        "limit": limit,
        "page": page,
        "pagesCount": pages,
    }


def listing_handler(
    pages: Callable[[int], dict[str, Any]],
    *,
    detail: dict[str, Any] | None = None,
) -> tuple[Handler, dict[str, int]]:
    """Route auth/list/detail calls and count how often each is hit."""
    resolved_detail = PAYMENT_RESPONSE if detail is None else detail
    state = {"auth": 0, "list": 0, "detail": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "POST" and path == "/v1/invoice":
            return httpx.Response(200, json=INVOICE_RESPONSE)
        if method == "POST" and path == "/v1/auth":
            state["auth"] += 1
            return httpx.Response(200, json={"token": f"jwt-{state['auth']}"})
        if method == "GET" and path == "/v1/payment/":
            state["list"] += 1
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=pages(page))
        if method == "GET" and path == f"/v1/payment/{PAYMENT_ID}":
            state["detail"] += 1
            return httpx.Response(200, json=resolved_detail)
        return httpx.Response(404, text="not found")

    return handler, state


def gateway_with(
    handler: Handler, *, auth: bool = True, **kwargs: Any
) -> tuple[NowPaymentsGateway, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    gateway = NowPaymentsGateway(
        API_KEY,
        callback_url=CALLBACK,
        auth_email=EMAIL if auth else None,
        auth_password=PASSWORD if auth else None,
        transport=httpx.MockTransport(record),
        **kwargs,
    )
    return gateway, requests


def invoice_posts(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in requests if r.method == "POST" and r.url.path == "/v1/invoice"]


def create_arguments(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "amount_minor": 1050,
        "currency": "USD",
        "reference": "cust-1",
        "idempotency_key": IdempotencyKey(MERCHANT_REF),
    }
    arguments.update(overrides)
    return arguments


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
async def test_create_payment_posts_official_invoice_payload() -> None:
    gateway, requests = gateway_with(lambda request: httpx.Response(200, json=INVOICE_RESPONSE))
    try:
        intent = await gateway.create_payment(**create_arguments())
    finally:
        await gateway.close()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url == "https://api.nowpayments.io/v1/invoice"
    assert request.headers["x-api-key"] == API_KEY

    content = request.content
    assert b'"price_amount":10.5' in content
    assert b'"price_amount":"' not in content
    body = json.loads(content)
    assert body == {
        "price_amount": 10.5,
        "price_currency": "usd",
        "order_id": ORDER_REF,
        "ipn_callback_url": CALLBACK,
        "is_fixed_rate": False,
        "is_fee_paid_by_user": False,
    }
    assert "pay_currency" not in body
    assert "success_url" not in body
    assert "cancel_url" not in body
    assert b"3xui" not in content

    assert intent.gateway_payment_id == INVOICE_ID
    assert intent.status is PaymentStatus.PENDING
    assert intent.amount_minor == 1050
    assert intent.currency == "USD"
    assert intent.redirect_url == INVOICE_URL
    assert intent.metadata["invoice_id"] == INVOICE_ID
    assert intent.metadata["invoice_url"] == INVOICE_URL
    assert intent.metadata["payment_url"] == INVOICE_URL
    assert intent.metadata["merchant_order_ref"] == MERCHANT_REF
    assert intent.metadata["provider_order_ref"] == ORDER_REF
    assert intent.metadata["currency"] == "USD"


@pytest.mark.parametrize(
    ("amount_minor", "token", "expected_value"),
    [
        (1000, b'"price_amount":10', 10),
        (999, b'"price_amount":9.99', 9.99),
        (1505, b'"price_amount":15.05', 15.05),
    ],
    ids=["1000", "999", "1505"],
)
async def test_create_payment_emits_exact_cent_tokens(
    amount_minor: int, token: bytes, expected_value: int | float
) -> None:
    price_text = {1000: "10", 999: "9.99", 1505: "15.05"}[amount_minor]
    echo = {**INVOICE_RESPONSE, "price_amount": price_text}
    gateway, requests = gateway_with(lambda request: httpx.Response(200, json=echo))
    try:
        await gateway.create_payment(**create_arguments(amount_minor=amount_minor))
    finally:
        await gateway.close()

    content = requests[0].content
    assert token in content
    parsed = json.loads(content)["price_amount"]
    assert not isinstance(parsed, str)
    assert isinstance(parsed, int | float)
    assert parsed == expected_value
    if isinstance(expected_value, float):
        assert isinstance(parsed, float)


@pytest.mark.parametrize(
    "overrides",
    [
        {"currency": "IRR"},
        {"amount_minor": 10.0},
        {"amount_minor": True},
        {"amount_minor": 0},
        {"reference": " "},
    ],
    ids=["currency", "float-amount", "bool-amount", "zero-amount", "blank-reference"],
)
async def test_create_payment_rejects_invalid_input_before_http(
    overrides: dict[str, Any],
) -> None:
    gateway, requests = gateway_with(lambda request: httpx.Response(200, json=INVOICE_RESPONSE))
    try:
        with pytest.raises(ValueError):
            await gateway.create_payment(**create_arguments(**overrides))
        assert requests == []
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"order_id": "other"},
        {"price_amount": "9.99"},
        {"price_amount": "10.505"},
        {"price_currency": "eur"},
        {"invoice_url": "http://x/y"},
        {"id": None},
    ],
    ids=["order", "amount", "precision", "currency", "url", "id"],
)
async def test_create_payment_validates_provider_response(changes: dict[str, Any]) -> None:
    response = {**INVOICE_RESPONSE, **changes}
    gateway, requests = gateway_with(lambda request: httpx.Response(200, json=response))
    try:
        with pytest.raises(ProviderError):
            await gateway.create_payment(**create_arguments())
        assert len(requests) == 1
    finally:
        await gateway.close()


async def test_create_payment_timeout_is_ambiguous_and_not_retried() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("fixture timeout", request=request)

    gateway, requests = gateway_with(handler)
    try:
        with pytest.raises(ProviderOutcomeUnknown) as raised:
            await gateway.create_payment(**create_arguments())
    finally:
        await gateway.close()

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/invoice"
    message = str(raised.value)
    assert API_KEY not in message
    assert "secret" not in message


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (404, ProviderNotFound),
        (429, ProviderRateLimited),
        (500, ProviderUnavailable),
        (503, ProviderUnavailable),
        (400, ProviderError),
    ],
)
async def test_create_payment_maps_http_status_safely(status: int, error: type[Exception]) -> None:
    gateway, _ = gateway_with(lambda request: httpx.Response(status, text="secret body"))
    try:
        with pytest.raises(error) as raised:
            await gateway.create_payment(**create_arguments())
    finally:
        await gateway.close()

    message = str(raised.value)
    assert "secret body" not in message
    assert API_KEY not in message


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
async def test_verify_with_reference_finished_payment_succeeds() -> None:
    handler, state = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]))
    gateway, requests = gateway_with(handler)
    try:
        intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert intent.status is PaymentStatus.SUCCEEDED
    assert intent.gateway_payment_id == INVOICE_ID
    assert intent.amount_minor == 1050
    assert intent.currency == "USD"
    assert intent.metadata["payment_id"] == PAYMENT_ID
    assert intent.metadata["merchant_order_ref"] == MERCHANT_REF
    assert intent.metadata["provider_order_ref"] == ORDER_REF
    assert intent.metadata["invoice_id"] == INVOICE_ID
    assert intent.metadata["provider_status"] == "finished"

    list_requests = [r for r in requests if r.method == "GET" and r.url.path == "/v1/payment/"]
    assert len(list_requests) == 1
    assert list_requests[0].headers["Authorization"] == "Bearer jwt-1"
    assert list_requests[0].headers["x-api-key"] == API_KEY
    assert state["auth"] == 1
    assert invoice_posts(requests) == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("waiting", PaymentStatus.PENDING),
        ("confirming", PaymentStatus.PENDING),
        ("confirmed", PaymentStatus.PENDING),
        ("sending", PaymentStatus.PENDING),
        ("partially_paid", PaymentStatus.PENDING),
        ("failed", PaymentStatus.FAILED),
        ("expired", PaymentStatus.FAILED),
        ("refunded", PaymentStatus.REFUNDED),
        ("some_future_status", PaymentStatus.PENDING),
        ("finished", PaymentStatus.SUCCEEDED),
    ],
)
async def test_verify_with_reference_maps_detail_status(
    status: str, expected: PaymentStatus
) -> None:
    detail = {**PAYMENT_RESPONSE, "payment_status": status}
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]), detail=detail)
    gateway, requests = gateway_with(handler)
    try:
        intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert intent.status is expected
    assert invoice_posts(requests) == []


@pytest.mark.parametrize(
    ("changes", "error_expected"),
    [
        ({"actually_paid": 0.0001}, True),
        ({"pay_amount": None, "actually_paid": None}, True),
        ({"payment_status": "partially_paid", "actually_paid": 0.0001}, False),
    ],
    ids=["underpaid", "missing-evidence", "non-finished"],
)
async def test_verify_finished_requires_full_payment_evidence(
    changes: dict[str, Any], error_expected: bool
) -> None:
    detail = {**PAYMENT_RESPONSE, **changes}
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]), detail=detail)
    gateway, _ = gateway_with(handler)
    try:
        if error_expected:
            with pytest.raises(ProviderError):
                await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
        else:
            intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
            assert intent.status is PaymentStatus.PENDING
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "entries",
    [[], [OTHER_ORDER_ENTRY]],
    ids=["empty-page", "other-order"],
)
async def test_verify_with_reference_without_match_stays_pending(
    entries: list[dict[str, Any]],
) -> None:
    handler, _ = listing_handler(lambda page: page_of(entries))
    gateway, requests = gateway_with(handler)
    try:
        intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert intent.status is PaymentStatus.PENDING
    assert intent.amount_minor == 1050
    assert intent.currency == "USD"
    assert intent.gateway_payment_id == INVOICE_ID
    assert intent.metadata["merchant_order_ref"] == MERCHANT_REF
    assert invoice_posts(requests) == []


async def test_verify_with_reference_rejects_multiple_matches() -> None:
    entries = [PAYMENT_RESPONSE, SECOND_MATCH_ENTRY]
    handler, _ = listing_handler(lambda page: page_of(entries))
    gateway, requests = gateway_with(handler)
    try:
        with pytest.raises(ProviderError):
            await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert invoice_posts(requests) == []


async def test_verify_with_reference_ignores_forged_or_missing_order_id() -> None:
    entries = [FORGED_ORDER_ENTRY, NULL_ORDER_ENTRY]
    handler, _ = listing_handler(lambda page: page_of(entries))
    gateway, requests = gateway_with(handler)
    try:
        intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert intent.status is PaymentStatus.PENDING
    assert intent.amount_minor == 1050
    assert invoice_posts(requests) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"invoice_id": "other-invoice"},
        {"order_id": "other"},
        {"payment_id": "507f1f77bcf86cd7994390ff"},
    ],
    ids=["invoice", "order", "payment"],
)
async def test_verify_concrete_rejects_detail_identity_mismatch(
    changes: dict[str, Any],
) -> None:
    detail = {**PAYMENT_RESPONSE, **changes}
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]), detail=detail)
    gateway, requests = gateway_with(handler)
    try:
        with pytest.raises(ProviderError):
            await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert invoice_posts(requests) == []


async def test_verify_with_reference_rejects_amount_mismatch() -> None:
    detail = {**PAYMENT_RESPONSE, "price_amount": "99.00"}
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]), detail=detail)
    gateway, _ = gateway_with(handler)
    try:
        with pytest.raises(ProviderError):
            await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()


async def test_invoice_id_is_never_requested_as_a_payment_id() -> None:
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]))
    gateway, requests = gateway_with(handler)
    try:
        await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    payment_paths = [
        r.url.path for r in requests if r.method == "GET" and r.url.path.startswith("/v1/payment/")
    ]
    assert f"/v1/payment/{PAYMENT_ID}" in payment_paths
    assert f"/v1/payment/{INVOICE_ID}" not in payment_paths


async def test_scan_paginates_until_match_found() -> None:
    payloads = {
        0: page_of([NO_MATCH_A, NO_MATCH_B], page=0, pages=3, limit=2),
        1: page_of([PAYMENT_RESPONSE], page=1, pages=3, limit=2),
        2: page_of([], page=2, pages=3, limit=2),
    }
    handler, _ = listing_handler(lambda page: payloads[page])
    gateway, requests = gateway_with(handler, page_size=2, max_scan_pages=3)
    try:
        intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert intent.status is PaymentStatus.SUCCEEDED
    assert invoice_posts(requests) == []


async def test_scan_incomplete_when_page_cap_is_reached() -> None:
    def payloads(page: int) -> dict[str, Any]:
        return page_of([NO_MATCH_A, NO_MATCH_B], page=page, pages=5, limit=2)

    handler, _ = listing_handler(payloads)
    gateway, requests = gateway_with(handler, page_size=2, max_scan_pages=3)
    try:
        with pytest.raises(ProviderUnavailable):
            await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert invoice_posts(requests) == []


async def test_short_page_does_not_override_pages_count() -> None:
    payloads = {
        0: page_of([NO_MATCH_A], page=0, pages=3, limit=2),
        1: page_of([], page=1, pages=3, limit=2),
        2: page_of([PAYMENT_RESPONSE], page=2, pages=3, limit=2),
    }
    handler, _ = listing_handler(lambda page: payloads[page])
    gateway, requests = gateway_with(handler, page_size=2, max_scan_pages=3)
    try:
        intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert intent.status is PaymentStatus.SUCCEEDED
    assert invoice_posts(requests) == []


async def test_verify_refreshes_token_once_on_401() -> None:
    state = {"auth": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "POST" and path == "/v1/auth":
            state["auth"] += 1
            return httpx.Response(200, json={"token": f"jwt-{state['auth']}"})
        if method == "GET" and path == "/v1/payment/":
            state["list"] += 1
            if state["list"] == 1:
                return httpx.Response(401, text="secret")
            return httpx.Response(200, json=page_of([PAYMENT_RESPONSE]))
        if method == "GET" and path == f"/v1/payment/{PAYMENT_ID}":
            return httpx.Response(200, json=PAYMENT_RESPONSE)
        return httpx.Response(404, text="not found")

    gateway, _ = gateway_with(handler)
    try:
        intent = await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert intent.status is PaymentStatus.SUCCEEDED
    assert state["auth"] == 2
    assert state["list"] == 2


async def test_verify_fails_closed_when_every_listing_call_is_rejected() -> None:
    state = {"auth": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        if method == "POST" and path == "/v1/auth":
            state["auth"] += 1
            return httpx.Response(200, json={"token": f"jwt-{state['auth']}"})
        if method == "GET" and path == "/v1/payment/":
            state["list"] += 1
            return httpx.Response(401, text="secret")
        return httpx.Response(404, text="not found")

    gateway, _ = gateway_with(handler)
    try:
        with pytest.raises(ProviderAuthError):
            await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert state["auth"] == 2


async def test_listing_without_credentials_requires_credentials() -> None:
    handler, _ = listing_handler(lambda page: page_of([]))
    gateway, requests = gateway_with(handler, auth=False)
    try:
        with pytest.raises(ProviderError) as raised:
            await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()

    assert "credentials" in str(raised.value).lower()
    assert requests == []
    assert invoice_posts(requests) == []


@pytest.mark.parametrize(
    ("entries", "expected_status", "expected_amount"),
    [
        ([PAYMENT_RESPONSE], PaymentStatus.SUCCEEDED, 1050),
        ([], PaymentStatus.PENDING, 0),
        ([{**PAYMENT_RESPONSE, "invoice_id": "other-invoice"}], PaymentStatus.PENDING, 0),
    ],
    ids=["match", "empty", "foreign-invoice"],
)
async def test_verify_payment_by_invoice_id(
    entries: list[dict[str, Any]], expected_status: PaymentStatus, expected_amount: int
) -> None:
    handler, _ = listing_handler(lambda page: page_of(entries))
    gateway, _ = gateway_with(handler)
    try:
        intent = await gateway.verify_payment(INVOICE_ID)
    finally:
        await gateway.close()

    assert intent.status is expected_status
    assert intent.gateway_payment_id == INVOICE_ID
    assert intent.amount_minor == expected_amount


async def test_get_payment_details_returns_concrete_facts() -> None:
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]))
    gateway, _ = gateway_with(handler)
    try:
        details = await gateway.get_payment_details(INVOICE_ID)
    finally:
        await gateway.close()

    assert details["currency"] == "USD"
    assert details["invoice_id"] == INVOICE_ID
    assert details["total_amount_minor"] == "1050"
    assert details["payment_id"] == PAYMENT_ID
    assert details["merchant_order_ref"] == MERCHANT_REF


async def test_get_payment_details_without_match_invents_nothing() -> None:
    handler, _ = listing_handler(lambda page: page_of([]))
    gateway, _ = gateway_with(handler)
    try:
        details = await gateway.get_payment_details(INVOICE_ID)
    finally:
        await gateway.close()

    assert details == {"currency": "USD", "invoice_id": INVOICE_ID}


async def test_get_payment_details_without_credentials_makes_no_http() -> None:
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]))
    gateway, requests = gateway_with(handler, auth=False)
    try:
        details = await gateway.get_payment_details(INVOICE_ID)
    finally:
        await gateway.close()

    assert details == {"currency": "USD", "invoice_id": INVOICE_ID}
    assert requests == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": ""},
        {"api_key": "   "},
        {"api_key": "changeme-00000000"},
        {"base_url": "http://api.nowpayments.io/v1"},
        {"base_url": "https://api.nowpayments.io/v1?x=1"},
        {"callback_url": ""},
        {"callback_url": "http://x/cb"},
        {"auth_email": EMAIL},
        {"auth_password": PASSWORD},
        {"timeout_seconds": 0},
        {"page_size": 0},
        {"page_size": 1001},
        {"max_scan_pages": 0},
    ],
    ids=[
        "blank-key",
        "spaces-key",
        "placeholder-key",
        "plaintext-base",
        "query-base",
        "blank-callback",
        "plaintext-callback",
        "email-without-password",
        "password-without-email",
        "zero-timeout",
        "zero-page-size",
        "oversized-page-size",
        "zero-scan-pages",
    ],
)
def test_gateway_constructor_rejects_unsafe_configuration(overrides: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {"api_key": API_KEY, "callback_url": CALLBACK}
    kwargs.update(overrides)
    with pytest.raises(ValueError):
        NowPaymentsGateway(**kwargs)


async def test_gateway_advertises_only_create_and_verify() -> None:
    gateway, _ = gateway_with(lambda request: httpx.Response(200, json=INVOICE_RESPONSE))
    try:
        assert gateway.key == "nowpayments"
        assert gateway.supported_currency == "USD"
        assert gateway.requires_durable_creation is True
        assert gateway.capabilities == frozenset(
            {GatewayCapability.CREATE_PAYMENT, GatewayCapability.VERIFY_PAYMENT}
        )
        with pytest.raises(UnsupportedGatewayOperation):
            await gateway.refund(
                gateway_payment_id="x",
                amount_minor=1,
                idempotency_key=IdempotencyKey(MERCHANT_REF),
            )
    finally:
        await gateway.close()

    assert GATEWAY_KEY == "nowpayments"
    assert SUPPORTED_CURRENCY == "USD"
    assert PRICE_CURRENCY == "usd"
    assert ORDER_ID_NAMESPACE == "cloud-server-seller"


@pytest.mark.parametrize("identity", [None, "", "../1", "1?x=2", True, 0, "01", " 1 "])
async def test_unsafe_invoice_identity_never_reaches_http(identity: Any) -> None:
    gateway, requests = gateway_with(lambda request: httpx.Response(500))
    try:
        with pytest.raises((ValueError, ProviderError)):
            await gateway.verify_payment(identity)
        assert requests == []
    finally:
        await gateway.close()


async def test_numeric_creation_identity_normalizes_to_string() -> None:
    gateway, _ = gateway_with(
        lambda request: httpx.Response(200, json={**INVOICE_RESPONSE, "id": int(INVOICE_ID)})
    )
    try:
        intent = await gateway.create_payment(**create_arguments())
        assert intent.gateway_payment_id == INVOICE_ID
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"pay_amount": 0, "actually_paid": 0},
        {"pay_amount": 0, "actually_paid": 1},
        {"pay_amount": 1, "actually_paid": 0},
    ],
)
async def test_finished_zero_evidence_never_succeeds(changes: dict[str, Any]) -> None:
    handler, _ = listing_handler(
        lambda page: page_of([PAYMENT_RESPONSE]), detail={**PAYMENT_RESPONSE, **changes}
    )
    gateway, _ = gateway_with(handler)
    try:
        with pytest.raises(ProviderError):
            await gateway.verify_with_reference(INVOICE_ID, 1050, MERCHANT_REF)
    finally:
        await gateway.close()


async def test_missing_invoice_in_listing_does_not_match_every_cloud_order() -> None:
    handler, state = listing_handler(
        lambda page: page_of([{**PAYMENT_RESPONSE, "invoice_id": None}])
    )
    gateway, _ = gateway_with(handler)
    try:
        assert (await gateway.verify_payment(INVOICE_ID)).status is PaymentStatus.PENDING
        assert state["detail"] == 0
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://nowpayments.io.attacker.test/invoice",
        "https://attacker.test/pay",
        "https://user:pass@nowpayments.io/payment/",
        "https://nowpayments.io/payment/#fragment",
        "https://nowpayments.io:bad/payment/",
    ],
)
async def test_hosted_invoice_rejects_unsafe_destinations(unsafe_url: str) -> None:
    gateway, _ = gateway_with(
        lambda request: httpx.Response(200, json={**INVOICE_RESPONSE, "invoice_url": unsafe_url})
    )
    try:
        with pytest.raises(ProviderError):
            await gateway.create_payment(**create_arguments())
    finally:
        await gateway.close()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -float("inf")])
def test_timeout_must_be_finite(timeout: float) -> None:
    with pytest.raises(ValueError):
        NowPaymentsGateway(API_KEY, callback_url=CALLBACK, timeout_seconds=timeout)


async def test_password_containing_todo_is_accepted() -> None:
    gateway = NowPaymentsGateway(
        API_KEY,
        callback_url=CALLBACK,
        auth_email=EMAIL,
        auth_password="strong-todo-embedded-password",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    await gateway.close()


async def test_ambiguous_recovery_is_read_only_and_validates_full_identity() -> None:
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]))
    gateway, requests = gateway_with(handler)
    try:
        intent = await gateway.recover_payment(amount_minor=1050, merchant_order_ref=MERCHANT_REF)
        assert intent is not None and intent.gateway_payment_id == INVOICE_ID
        assert intent.metadata["payment_id"] == PAYMENT_ID
        assert not invoice_posts(requests)
    finally:
        await gateway.close()


async def test_verify_payment_session_integration_contract() -> None:
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]))
    gateway, _ = gateway_with(handler)
    session = PaymentSession(
        user_id=uuid4(),
        gateway_key="nowpayments",
        gateway_payment_id=INVOICE_ID,
        amount_minor=1050,
        currency="USD",
        idempotency_key=MERCHANT_REF,
    )
    try:
        intent = await verify_payment_session(gateway, session)
    finally:
        await gateway.close()

    assert intent.status is PaymentStatus.SUCCEEDED
    assert intent.amount_minor == 1050
    assert intent.currency == "USD"
    assert intent.gateway_payment_id == INVOICE_ID
