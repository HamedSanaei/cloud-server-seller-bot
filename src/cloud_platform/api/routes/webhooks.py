"""Signed webhook callbacks from payment gateways.

The route verifies the per-gateway HMAC signature over the RAW body, then
delegates to the replay-safe :class:`PaymentWebhookService`. Signature
verification happens at the edge; deposit deduplication happens in the
service and the database.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from cloud_platform.core.config import get_settings
from cloud_platform.core.container import (
    get_nowpayments_callback_service,
    get_payment_webhook_service,
)
from cloud_platform.core.webhooks import verify_gateway_signature
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    InvalidPaymentSessionTransition,
)
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.providers.nowpayments.callbacks import NowPaymentsCallbackService
from cloud_platform.providers.nowpayments.signatures import SignatureError, verify_ipn_signature

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SIGNATURE_HEADER = "x-gateway-signature"


async def get_gateway_payment_secret(gateway_key: str) -> str:
    """Resolve the shared secret for a gateway; 404 for unknown gateways."""
    # AtlasPay has no trusted signed-callback contract: only provider GET
    # verification in its polling reconciler may settle an AtlasPay order.
    if gateway_key in {"atlaspay", "nowpayments"}:
        raise HTTPException(status_code=404, detail="unknown gateway")
    settings = get_settings()
    secret = settings.payment_gateway_secrets.get(gateway_key)
    if not secret:
        raise HTTPException(status_code=404, detail="unknown gateway")
    return secret


def _parse_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="payload must be a JSON object")
    return payload


def _parse_user_id(payload: dict[str, Any]) -> uuid.UUID | None:
    raw = payload.get("user_id")
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="user_id must be a UUID") from exc


def _parse_amount(payload: dict[str, Any]) -> int | None:
    raw = payload.get("amount_minor")
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise HTTPException(status_code=400, detail="amount_minor must be an integer")
    return raw


MAX_IPN_BYTES = 64 * 1024


@router.post("/payments/nowpayments")
async def nowpayments_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    service: Annotated[NowPaymentsCallbackService, Depends(get_nowpayments_callback_service)],
) -> dict[str, str]:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_IPN_BYTES:
            raise HTTPException(status_code=413, detail="IPN body too large")
    secret = get_settings().nowpayments_ipn_secret
    if not secret:
        raise HTTPException(status_code=404, detail="unknown gateway")
    try:
        payload = verify_ipn_signature(body, request.headers.get("x-nowpayments-sig", ""), secret)
    except SignatureError as exc:
        raise HTTPException(status_code=401, detail="invalid IPN signature") from exc
    session = await service.identify_session(payload)
    if session is None:
        return {"action": "ignored"}
    # Acknowledgement is backed by the original durable pending attempt. The
    # worker scans bound AND unbound NOWPayments attempts after process loss.
    background_tasks.add_task(service.process_callback, payload)
    return {"action": "accepted"}


@router.post("/payments/{gateway_key}")
async def payment_webhook(
    request: Request,
    gateway_key: str,
    service: Annotated[PaymentWebhookService, Depends(get_payment_webhook_service)],
    secret: Annotated[str, Depends(get_gateway_payment_secret)],
) -> dict[str, str]:
    if gateway_key in {"atlaspay", "nowpayments"}:
        raise HTTPException(status_code=404, detail="unknown gateway")
    body = await request.body()
    signature = request.headers.get(SIGNATURE_HEADER, "")
    if not verify_gateway_signature(secret, body, signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = _parse_payload(body)
    try:
        outcome = await service.process_callback(
            gateway_key=gateway_key,
            external_id=str(payload.get("gateway_payment_id", "")),
            status=str(payload.get("status", "")),
            user_id=_parse_user_id(payload),
            amount_minor=_parse_amount(payload),
            currency=payload.get("currency"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (DuplicateExternalIdError, InvalidPaymentSessionTransition) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"action": outcome.action.value, "session_status": outcome.session.status.value}
