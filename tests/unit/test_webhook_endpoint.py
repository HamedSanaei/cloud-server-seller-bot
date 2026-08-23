"""Tests for the signed payment webhook HTTP endpoint (M09-003)."""

from __future__ import annotations

import dataclasses
import json
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from cloud_platform.api.app import create_app
from cloud_platform.api.routes.webhooks import get_gateway_payment_secret
from cloud_platform.core.container import get_payment_webhook_service
from cloud_platform.core.webhooks import sign_gateway_payload
from cloud_platform.modules.payments.service import PaymentWebhookService

SECRET = "endpoint-test-secret"
USER_ID = uuid4()
EXT = "ext-1"


def _service() -> tuple[PaymentWebhookService, AsyncMock, AsyncMock, AsyncMock]:
    payments = AsyncMock()
    payments.get_by_external_id = AsyncMock(return_value=None)
    payments.create = AsyncMock(side_effect=lambda s: dataclasses.replace(s, id=uuid4()))
    payments.save = AsyncMock(side_effect=lambda s: s)
    wallet = AsyncMock()
    ledger = AsyncMock()
    ledger.get_entry_by_idempotency = AsyncMock(return_value=None)
    return (
        PaymentWebhookService(payments, wallet, ledger),
        payments,
        wallet,
        ledger,
    )


def _build_app(service: PaymentWebhookService, secret: str | None = SECRET) -> FastAPI:
    app = create_app()
    app.dependency_overrides[get_payment_webhook_service] = lambda: service

    def _secret(gateway_key: str = "") -> str:
        if secret is None:
            raise HTTPException(status_code=404, detail="unknown gateway")
        return secret

    app.dependency_overrides[get_gateway_payment_secret] = _secret
    return app


def _body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "gateway_payment_id": EXT,
        "status": "succeeded",
        "user_id": str(USER_ID),
        "amount_minor": 500,
        "currency": "EUR",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _post(client: TestClient, body: bytes, signature: str | None) -> object:
    headers = {"x-gateway-signature": signature} if signature is not None else {}
    return client.post("/webhooks/payments/zarinpal", content=body, headers=headers)


class TestAuthentication:
    def test_unknown_gateway_returns_404(self) -> None:
        service, _p, _w, _l = _service()
        client = TestClient(_build_app(service, secret=None))
        body = _body()
        response = _post(client, body, sign_gateway_payload(SECRET, body))
        assert response.status_code == 404

    def test_missing_signature_returns_401(self) -> None:
        service, _p, _w, _l = _service()
        client = TestClient(_build_app(service))
        response = _post(client, _body(), None)
        assert response.status_code == 401

    def test_invalid_signature_returns_401(self) -> None:
        service, _p, _w, _l = _service()
        client = TestClient(_build_app(service))
        response = _post(client, _body(), "deadbeef")
        assert response.status_code == 401

    def test_signature_tamper_evident(self) -> None:
        service, _p, _w, _l = _service()
        client = TestClient(_build_app(service))
        honest = _body(amount_minor=500)
        sig = sign_gateway_payload(SECRET, honest)
        dishonest = _body(amount_minor=50000)  # attacker inflates the amount
        response = _post(client, dishonest, sig)
        assert response.status_code == 401


class TestCallbackProcessing:
    def test_first_callback_credits(self) -> None:
        service, _p, wallet, _l = _service()
        client = TestClient(_build_app(service))
        body = _body()
        response = _post(client, body, sign_gateway_payload(SECRET, body))
        assert response.status_code == 200
        assert response.json() == {"action": "credited", "session_status": "succeeded"}
        wallet.add_funds.assert_awaited_once()

    def test_replayed_callback_cannot_duplicate_deposit(self) -> None:
        """The core acceptance: duplicate callback cannot duplicate deposit."""
        # Stateful mock: the session saved by the first delivery is what the
        # second delivery reads back, mirroring durable storage.
        payments = AsyncMock()
        state: dict[str, object] = {}
        payments.get_by_external_id = AsyncMock(side_effect=lambda g, e: state.get("session"))
        payments.create = AsyncMock(side_effect=lambda s: dataclasses.replace(s, id=uuid4()))
        payments.save = AsyncMock(side_effect=lambda s: state.update({"session": s}) or s)
        wallet = AsyncMock()
        ledger = AsyncMock()
        ledger.get_entry_by_idempotency = AsyncMock(return_value=None)
        service = PaymentWebhookService(payments, wallet, ledger)

        client = TestClient(_build_app(service))
        body = _body()
        sig = sign_gateway_payload(SECRET, body)

        first = _post(client, body, sig)
        assert first.status_code == 200
        assert first.json()["action"] == "credited"

        # The very same callback arrives again (gateway retry).
        second = _post(client, body, sig)
        assert second.status_code == 200
        assert second.json()["action"] == "duplicate_ignored"
        # The wallet was credited exactly once across both deliveries.
        wallet.add_funds.assert_awaited_once()

    def test_invalid_json_returns_400(self) -> None:
        service, _p, _w, _l = _service()
        client = TestClient(_build_app(service))
        body = b"not-json"
        response = _post(client, body, sign_gateway_payload(SECRET, body))
        assert response.status_code == 400

    def test_bad_user_id_returns_400(self) -> None:
        service, _p, _w, _l = _service()
        client = TestClient(_build_app(service))
        body = _body(user_id="not-a-uuid")
        response = _post(client, body, sign_gateway_payload(SECRET, body))
        assert response.status_code == 400

    def test_float_amount_returns_400(self) -> None:
        service, _p, _w, _l = _service()
        client = TestClient(_build_app(service))
        body = _body(amount_minor=500.5)
        response = _post(client, body, sign_gateway_payload(SECRET, body))
        assert response.status_code == 400
