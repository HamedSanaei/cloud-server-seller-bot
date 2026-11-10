"""TabaPay browser returns and authenticated automatic confirmations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from cloud_platform.api.routes.webhooks import MAX_IPN_BYTES, _parse_payload
from cloud_platform.core.config import get_settings
from cloud_platform.core.container import get_container, get_payment_webhook_service
from cloud_platform.modules.payments.domain import (
    InvalidPaymentSessionTransition,
    PaymentSession,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.inquiry import AuthoritativePaymentService
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.providers.errors import ProviderError
from cloud_platform.providers.tabapay.client import TabaPayGateway, integer, valid_token

router = APIRouter(prefix="/webhooks/payments/tabapay", tags=["webhooks"])


async def get_tabapay_gateway() -> AsyncIterator[TabaPayGateway]:
    settings = get_settings()
    if not settings.tabapay_merchant_id:
        raise HTTPException(status_code=404, detail="unknown gateway")
    gateway = TabaPayGateway.from_settings(settings)
    try:
        yield gateway
    finally:
        await gateway.close()


async def get_tabapay_payments() -> SqlAlchemyPaymentSessionRepository:
    container = await get_container()
    return SqlAlchemyPaymentSessionRepository(container.session_factory)


Gateway = Annotated[TabaPayGateway, Depends(get_tabapay_gateway)]
Payments = Annotated[SqlAlchemyPaymentSessionRepository, Depends(get_tabapay_payments)]
Settlement = Annotated[PaymentWebhookService, Depends(get_payment_webhook_service)]


async def _known_payment(
    payments: SqlAlchemyPaymentSessionRepository, token: object
) -> PaymentSession:
    if not isinstance(token, str) or not valid_token(token):
        raise HTTPException(status_code=400, detail="invalid payment token")
    session = await payments.get_by_external_id(TabaPayGateway.key, token)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown payment")
    return session


@router.get("")
async def tabapay_return(
    token: Annotated[str, Query(min_length=36, max_length=36)],
    gateway: Gateway,
    payments: Payments,
    settlement: Settlement,
) -> RedirectResponse:
    """Browser fields are hints. Verify the persisted token and Rial total."""
    session = await _known_payment(payments, token)
    service = AuthoritativePaymentService(
        payments_repo=payments, webhook_service=settlement, gateways={gateway.key: gateway}
    )
    try:
        await service.reconcile_session(session)
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail="payment verification unavailable") from exc
    return RedirectResponse(
        get_settings().tabapay_return_url, status_code=303, headers={"Cache-Control": "no-store"}
    )


@router.post("")
async def tabapay_notification(
    request: Request, gateway: Gateway, payments: Payments, settlement: Settlement
) -> dict[str, str]:
    # The docs name the PHP CGI variable HTTP_AUTHORIZE. The corresponding
    # wire header is Authorize. Accept the literal documented name as well.
    signature = request.headers.get("authorize") or request.headers.get("http_authorize", "")
    if not gateway.authentic_notification(signature):
        raise HTTPException(status_code=401, detail="invalid payment notification credential")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_IPN_BYTES:
            raise HTTPException(status_code=413, detail="payment notification too large")
        body.extend(chunk)
    payload = _parse_payload(bytes(body))
    session = await _known_payment(payments, payload.get("token"))
    reference = payload.get("shaparakRefNumber")
    if (
        integer(payload.get("responseCode")) != 1
        or payload.get("status") != "success"
        or integer(payload.get("amount")) != session.amount_minor
        or session.currency != gateway.supported_currency
        or not isinstance(reference, str)
        or not 0 < len(reference) <= 100
        or not reference.isascii()
        or not reference.isdecimal()
        or not reference.strip("0")
    ):
        raise HTTPException(
            status_code=400, detail="notification does not match the stored payment"
        )
    try:
        outcome = await settlement.process_callback(
            gateway_key=gateway.key,
            external_id=session.gateway_payment_id or "",
            status="succeeded",
            amount_minor=session.amount_minor,
            currency=session.currency,
        )
    except (InvalidPaymentSessionTransition, ValueError) as exc:
        raise HTTPException(
            status_code=409, detail="payment is not eligible for settlement"
        ) from exc
    if (
        outcome.session.status is not PaymentSessionStatus.SUCCEEDED
        or not outcome.session.credited_at
    ):
        raise HTTPException(status_code=503, detail="payment credit has not completed")
    return {"status": "success"}
