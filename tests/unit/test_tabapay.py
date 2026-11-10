"""Provider-shaped fixtures for TabaPay's payment and settlement boundaries."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import ValidationError

from cloud_platform.api.routes import tabapay
from cloud_platform.core.config import Settings
from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.payments.domain import PaymentSession
from cloud_platform.modules.payments.recharge import WalletRechargeService
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.providers.base import PaymentStatus
from cloud_platform.providers.errors import ProviderError
from cloud_platform.providers.tabapay.client import ORIGIN, TabaPayGateway

MERCHANT = str(uuid4())
TOKEN = str(uuid4())
CALLBACK_PATH = "/webhooks/payments/tabapay"
CALLBACK = f"https://merchant.example{CALLBACK_PATH}"
RETURN = "https://merchant.example/charge-requests"


def _confirmation(**overrides):
    return {
        "responseCode": 1,
        "status": "success",
        "token": TOKEN,
        "amount": "100000",
        "finalAmount": 99_000,
        "shaparakRefNumber": "111122223333",
        "trackingCode": 40,
        **overrides,
    }


@pytest.fixture
async def gateway():
    adapter = TabaPayGateway(
        merchant_id=MERCHANT, callback_url=CALLBACK, sandbox=True, timeout_seconds=2
    )
    try:
        yield adapter
    finally:
        await adapter.close()


@respx.mock
async def test_create_uses_multipart_rial_and_refuses_an_untrusted_redirect(gateway):
    endpoint = respx.post(f"{ORIGIN}/v1/sandbox/create")
    pay_url = f"{ORIGIN}/sandbox/pay/{TOKEN}"
    endpoint.mock(return_value=httpx.Response(200, json=_confirmation(url=pay_url)))
    options = {
        "amount_minor": 100_000,
        "currency": "IRR",
        "reference": "local-wallet",
        "idempotency_key": IdempotencyKey("local-payment-attempt"),
        "redirect_url": "https://untrusted.example",
    }
    intent = await gateway.create_payment(**options)
    request = endpoint.calls.last.request
    assert request.headers["authorization"] == f"Bearer {MERCHANT}"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="amount"\r\n\r\n100000\r\n' in request.content
    assert CALLBACK.encode() in request.content
    assert b"untrusted.example" not in request.content
    assert intent.redirect_url == pay_url
    assert intent.status is PaymentStatus.PENDING

    with pytest.raises(ValueError):
        await gateway.create_payment(**(options | {"amount_minor": 99_999}))
    assert endpoint.call_count == 1
    endpoint.mock(
        return_value=httpx.Response(200, json=_confirmation(url="https://untrusted.example"))
    )
    with pytest.raises(ProviderError, match="unexpected payment URL"):
        await gateway.create_payment(**options)


@respx.mock
async def test_verification_requires_matching_gross_amount_and_token(gateway):
    endpoint = respx.post(f"{ORIGIN}/v1/sandbox/verify")
    endpoint.mock(return_value=httpx.Response(200, json=_confirmation(responseCode=2)))
    intent = await gateway.verify_with_amount(TOKEN, 100_000)
    assert intent.status is PaymentStatus.SUCCEEDED
    assert intent.amount_minor == 100_000
    assert b'name="token"\r\n\r\n' + TOKEN.encode() in endpoint.calls.last.request.content

    endpoint.mock(return_value=httpx.Response(200, json=_confirmation(amount="99000")))
    with pytest.raises(ProviderError, match="does not match"):
        await gateway.verify_with_amount(TOKEN, 100_000)
    endpoint.mock(
        return_value=httpx.Response(200, json=_confirmation(responseCode=-101, status="error"))
    )
    assert (await gateway.verify_with_amount(TOKEN, 100_000)).status is PaymentStatus.PENDING


@respx.mock
async def test_recharge_persists_toman_credit_before_creating_one_rial_invoice(gateway):
    stored = None

    def save(session):
        nonlocal stored
        stored = session
        return stored

    payments = AsyncMock()
    payments.get_by_idempotency_key.side_effect = lambda *_: stored
    payments.get.side_effect = lambda _: stored
    payments.create.side_effect = lambda session: save(replace(session, id=uuid4()))
    payments.save.side_effect = save
    service = WalletRechargeService(payments_repo=payments, gateways={gateway.key: gateway})
    pay_url = f"{ORIGIN}/sandbox/pay/{TOKEN}"

    def issue(request):
        assert stored is not None and stored.gateway_payment_id is None
        assert (stored.amount_minor, stored.currency) == (100_000, "IRR")
        assert (stored.credit_amount_minor, stored.credit_currency) == (10_000, "IRT")
        assert b'name="amount"\r\n\r\n100000\r\n' in request.content
        return httpx.Response(200, json=_confirmation(url=pay_url))

    endpoint = respx.post(f"{ORIGIN}/v1/sandbox/create").mock(side_effect=issue)
    options = {
        "user": SimpleNamespace(id=uuid4()),
        "amount_minor": 10_000,
        "currency": "IRT",
        "idempotency_key": "toman-recharge",
        "gateway_key": gateway.key,
    }
    first = await service.start(**options)
    replay = await service.start(**options)
    assert first.redirect_url == replay.redirect_url == pay_url
    assert replay.replayed
    assert endpoint.call_count == 1


@pytest.fixture
async def callback(gateway, monkeypatch):
    session = PaymentSession(
        id=uuid4(),
        user_id=uuid4(),
        gateway_key="tabapay",
        amount_minor=100_000,
        currency="IRR",
        credit_amount_minor=10_000,
        credit_currency="IRT",
        idempotency_key="local-payment-attempt",
        gateway_payment_id=TOKEN,
    )

    def save(updated):
        nonlocal session
        session = updated
        return updated

    payments = AsyncMock()
    payments.get_by_external_id.side_effect = lambda key, token: (
        session if key == "tabapay" and token == TOKEN else None
    )
    payments.save.side_effect = save
    wallet = AsyncMock()
    wallet.get.return_value = SimpleNamespace(currency="IRT")
    wallet.credit_deposit.return_value = (SimpleNamespace(balance=10_000), True)
    settlement = PaymentWebhookService(payments, wallet, AsyncMock())
    app = FastAPI()
    app.include_router(tabapay.router)
    app.dependency_overrides[tabapay.get_tabapay_gateway] = lambda: gateway
    app.dependency_overrides[tabapay.get_tabapay_payments] = lambda: payments
    app.dependency_overrides[tabapay.get_payment_webhook_service] = lambda: settlement
    monkeypatch.setattr(tabapay, "get_settings", lambda: SimpleNamespace(tabapay_return_url=RETURN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://merchant.example"
    ) as client:
        yield client, wallet.credit_deposit


def _notification_headers():
    return {"Authorize": hashlib.md5(MERCHANT.encode(), usedforsecurity=False).hexdigest()}


@respx.mock
async def test_browser_return_and_automatic_replays_credit_the_frozen_wallet_once(callback):
    client, credit = callback
    endpoint = respx.post(f"{ORIGIN}/v1/sandbox/verify")
    endpoint.mock(return_value=httpx.Response(200, json=_confirmation()))
    # A browser-supplied amount or state can neither change the deposit nor
    # cancel the issuer-confirmed transaction.
    result = await client.get(
        CALLBACK_PATH, params={"token": TOKEN, "amount": "1", "status": "error"}
    )
    assert result.status_code == 303
    assert result.headers["location"] == RETURN
    assert b'name="amount"\r\n\r\n100000\r\n' in endpoint.calls.last.request.content
    for _ in range(2):
        result = await client.post(
            CALLBACK_PATH, json=_confirmation(), headers=_notification_headers()
        )
        assert result.json() == {"status": "success"}
    credit.assert_awaited_once()
    assert credit.await_args.args[1:] == (10_000, f"deposit-tabapay-{TOKEN}")
    assert credit.await_args.kwargs["expected_currency"] == "IRT"


async def test_notification_requires_authentication_known_token_and_exact_amount(callback):
    client, credit = callback
    assert (await client.post(CALLBACK_PATH, json=_confirmation())).status_code == 401
    headers = _notification_headers()
    for overrides, status in (
        ({"amount": 10_000}, 400),
        ({"token": str(uuid4())}, 404),
        ({"shaparakRefNumber": ""}, 400),
    ):
        result = await client.post(CALLBACK_PATH, json=_confirmation(**overrides), headers=headers)
        assert result.status_code == status
    credit.assert_not_awaited()
    assert (await client.post(CALLBACK_PATH, json=_confirmation(), headers=headers)).json() == {
        "status": "success"
    }
    credit.assert_awaited_once()


def test_settings_fail_closed_for_bad_configuration_and_production_sandbox():
    config = {
        "tabapay_enabled": True,
        "tabapay_merchant_id": MERCHANT,
        "tabapay_callback_url": CALLBACK,
        "tabapay_return_url": RETURN,
    }
    assert Settings(**config).tabapay_enabled
    with pytest.raises(ValidationError, match="merchant_id"):
        Settings(**(config | {"tabapay_merchant_id": ""}))
    with pytest.raises(ValidationError, match="native callback path"):
        Settings(**(config | {"tabapay_callback_url": "https://merchant.example/other"}))
    with pytest.raises(ValidationError, match="sandbox is unavailable in production"):
        Settings(
            **config, app_env="production", telegram_sessions_backend="redis", tabapay_sandbox=True
        )
