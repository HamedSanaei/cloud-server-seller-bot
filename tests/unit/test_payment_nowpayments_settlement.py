"""Native IPN, manual inquiry and polling share authoritative settlement."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_nowpayments_gateway import (
    INVOICE_ID,
    MERCHANT_REF,
    ORDER_REF,
    PAYMENT_ID,
    PAYMENT_RESPONSE,
    gateway_with,
    invoice_posts,
    listing_handler,
    page_of,
)

from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.payments.inquiry import (
    AuthoritativePaymentService,
    PaymentInquiryService,
)
from cloud_platform.modules.payments.recharge import WalletRechargeService
from cloud_platform.modules.payments.reconcile import PaymentReconciliationService
from cloud_platform.modules.payments.repository import _validate_save
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.providers.nowpayments.callbacks import NowPaymentsCallbackService
from cloud_platform.providers.nowpayments.signatures import canonical_json


class MemoryPayments:
    def __init__(self, session):
        self.session = session

    async def get(self, session_id):
        return self.session if session_id == self.session.id else None

    async def get_by_external_id(self, gateway_key, external_id):
        return (
            self.session
            if (
                gateway_key == self.session.gateway_key
                and external_id == self.session.gateway_payment_id
            )
            else None
        )

    async def get_by_idempotency_key(self, gateway_key, key):
        return self.session if gateway_key == "nowpayments" and key == MERCHANT_REF else None

    async def save(self, session):
        _validate_save(self.session, session)
        if self.session.credited_at is not None:
            return self.session
        self.session = session
        return session

    async def list_pending_before(self, *args):
        return [self.session]


class MemoryWallet:
    def __init__(self):
        self.balance = 7
        self.entries = {}

    async def get(self, owner):
        return SimpleNamespace(currency="EUR")

    async def credit_deposit(self, owner, amount, key, **kwargs):
        if key not in self.entries:
            self.entries[key] = amount
            self.balance += amount


def system(*, detail=None, unbound=False):
    session = PaymentSession(
        id=uuid4(),
        user_id=uuid4(),
        gateway_key="nowpayments",
        amount_minor=1050,
        currency="USD",
        idempotency_key=MERCHANT_REF,
        gateway_payment_id=None if unbound else INVOICE_ID,
        credit_amount_minor=900,
        credit_currency="EUR",
        created_at=datetime.now(UTC),
    )
    payments = MemoryPayments(session)
    wallet = MemoryWallet()
    handler, _ = listing_handler(lambda page: page_of([PAYMENT_RESPONSE]), detail=detail)
    gateway, requests = gateway_with(handler)
    webhook = PaymentWebhookService(payments, wallet, AsyncMock())
    authoritative = AuthoritativePaymentService(
        payments_repo=payments, webhook_service=webhook, gateways={"nowpayments": gateway}
    )
    callback = NowPaymentsCallbackService(
        payments_repo=payments, authoritative_service=authoritative
    )
    return payments, wallet, gateway, requests, webhook, authoritative, callback


async def test_native_poll_manual_concurrent_replays_share_one_frozen_deposit():
    payments, wallet, gateway, requests, webhook, authoritative, callback = system()
    original = payments.session
    inquiry = PaymentInquiryService(
        payments_repo=payments, webhook_service=webhook, gateways={"nowpayments": gateway}
    )
    polling = PaymentReconciliationService(
        payments_repo=payments, webhook_service=webhook, gateway=gateway
    )
    payload = {
        "invoice_id": int(INVOICE_ID),
        "order_id": ORDER_REF,
        "user_id": str(uuid4()),
        "amount": 9999999,
        "payment_status": "failed",
    }
    try:
        await asyncio.gather(
            callback.process_callback(payload),
            polling.run(),
            inquiry.check_status(SimpleNamespace(id=original.user_id), original.id),
        )
        await authoritative.reconcile_session(payments.session)
        assert payments.session.status is PaymentSessionStatus.SUCCEEDED
        assert payments.session.gateway_payment_id == INVOICE_ID
        assert payments.session.provider_payment_id == PAYMENT_ID
        assert wallet.balance == 907
        assert list(wallet.entries.values()) == [900]
        assert not invoice_posts(requests)
    finally:
        await gateway.close()


@pytest.mark.parametrize(
    "detail",
    [
        {**PAYMENT_RESPONSE, "price_amount": "10.49"},
        {**PAYMENT_RESPONSE, "order_id": "other-project:attempt"},
        {**PAYMENT_RESPONSE, "payment_status": "partially_paid"},
        {**PAYMENT_RESPONSE, "pay_amount": 0, "actually_paid": 0},
        {**PAYMENT_RESPONSE, "actually_paid": "0.000001"},
    ],
)
async def test_signed_callback_cannot_override_authoritative_mismatch_or_partial(detail):
    payments, wallet, gateway, _, _, _, callback = system(detail=detail)
    try:
        await callback.process_callback(
            {"invoice_id": INVOICE_ID, "order_id": ORDER_REF, "payment_status": "finished"}
        )
        assert payments.session.status is PaymentSessionStatus.PENDING
        assert wallet.balance == 7 and not wallet.entries
    finally:
        await gateway.close()


async def test_unknown_foreign_and_wrong_invoice_callbacks_cannot_create_or_credit():
    payments, wallet, gateway, requests, _, _, callback = system()
    try:
        for payload in [
            {"invoice_id": INVOICE_ID, "order_id": "foreign:attempt"},
            {"invoice_id": INVOICE_ID, "order_id": "cloud-server-seller:unknown"},
            {"invoice_id": "777", "order_id": ORDER_REF},
            {"order_id": ORDER_REF},
        ]:
            assert await callback.process_callback(payload) == "ignored"
        assert wallet.balance == 7 and not requests
        assert payments.session.status is PaymentSessionStatus.PENDING
    finally:
        await gateway.close()


async def test_unbound_attempt_recovery_uses_reads_and_never_issues_invoice_again():
    payments, wallet, gateway, requests, _, authoritative, _ = system(unbound=True)
    try:
        await authoritative.reconcile_session(payments.session)
        assert payments.session.gateway_payment_id == INVOICE_ID
        assert payments.session.provider_payment_id == PAYMENT_ID
        assert wallet.balance == 907
        assert not invoice_posts(requests)
    finally:
        await gateway.close()


async def test_concrete_payment_binding_is_immutable():
    payments, _, gateway, _, _, _, _ = system()
    try:
        payments.session = payments.session.bind_provider_payment(PAYMENT_ID)
        with pytest.raises(ValueError, match="immutable"):
            await payments.save(replace(payments.session, provider_payment_id="777"))
    finally:
        await gateway.close()


async def test_disabled_creation_preserves_old_invoice_authoritative_settlement():
    payments, wallet, gateway, _, webhook, _, _ = system()
    recharge = WalletRechargeService(
        payments_repo=payments,
        gateways={"nowpayments": gateway},
        gateway_defaults={"nowpayments": False},
    )
    try:
        assert await recharge.compatible_gateways_async("USD", 1050) == []
        inquiry = PaymentInquiryService(
            payments_repo=payments, webhook_service=webhook, gateways={"nowpayments": gateway}
        )
        saved = await inquiry.check_status(
            SimpleNamespace(id=payments.session.user_id), payments.session.id
        )
        assert saved.credited_at is not None and wallet.balance == 907
    finally:
        await gateway.close()


async def test_native_route_bounded_signature_and_generic_sha256_refusal(monkeypatch):
    from cloud_platform.api.routes import webhooks

    service = SimpleNamespace(identify_session=AsyncMock(return_value=None))
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(
            nowpayments_ipn_secret="fixture-ipn-secret",  # pragma: allowlist secret
            payment_gateway_secrets={"nowpayments": "unsafe-generic-sha256-secret"},
        ),
    )
    app = FastAPI()
    app.include_router(webhooks.router)
    app.dependency_overrides[webhooks.get_nowpayments_callback_service] = lambda: service
    raw = b'{"invoice_id":123456789,"order_id":"cloud-server-seller:unknown"}'
    signature = hmac.new(
        b"fixture-ipn-secret", canonical_json(raw).encode(), hashlib.sha512
    ).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://app.test"
    ) as client:
        response = await client.post(
            "/webhooks/payments/nowpayments", content=raw, headers={"x-nowpayments-sig": signature}
        )
        assert response.status_code == 200 and response.json() == {"action": "ignored"}
        assert (
            await client.post(
                "/webhooks/payments/nowpayments",
                content=raw,
                headers={"x-gateway-signature": "0" * 64},
            )
        ).status_code == 401
        assert (
            await client.post(
                "/webhooks/payments/nowpayments", content=b"x" * (webhooks.MAX_IPN_BYTES + 1)
            )
        ).status_code == 413
    with pytest.raises(Exception) as exc:
        await webhooks.get_gateway_payment_secret("nowpayments")
    assert exc.value.status_code == 404
