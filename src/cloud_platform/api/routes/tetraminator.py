"""Tetraminator GET callback edge (unsigned by design).

Tetraminator documents NO webhook signature: after a successful payment it
performs ``GET <callback_url>`` with no body. The callback URL we send at
invoice creation carries only an opaque local session reference
(``?ref=<session-uuid>``) — no user id, no amount, no secret.

The callback itself is UNTRUSTED and never credits directly: this route
resolves the local session and delegates to
:class:`TetraminatorCallbackService`, which verifies the payment with a
server-side inquiry (``status is true`` AND ``payment_status == "paid"``
AND exact pay_id AND exact amount) before invoking the replay-safe
deposit path. Invalid references and unknown sessions fail safely without
revealing anything.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from cloud_platform.core.config import get_settings
from cloud_platform.core.container import get_payment_webhook_service
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.payments.service import (
    PaymentWebhookService,
    TetraminatorCallbackService,
)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def get_tetraminator_gateway() -> Any:
    """Build the Tetraminator adapter from server-owned configuration."""
    from cloud_platform.providers.tetraminator.client import TetraminatorGateway

    settings = get_settings()
    if not settings.tetraminator_enabled or not settings.tetraminator_api_key:
        raise HTTPException(status_code=503, detail="tetraminator gateway is not configured")
    gateway = TetraminatorGateway(
        api_key=settings.tetraminator_api_key,
        base_url=settings.tetraminator_base_url,
        callback_url=settings.tetraminator_callback_url,
        timeout_seconds=settings.tetraminator_timeout_seconds,
        require_https_callback=(settings.app_env or "").strip().lower() == "production",
    )
    try:
        yield gateway
    finally:
        await gateway.close()


async def get_tetraminator_callback_service(
    webhook: Annotated[PaymentWebhookService, Depends(get_payment_webhook_service)],
    gateway: Annotated[Any, Depends(get_tetraminator_gateway)],
) -> TetraminatorCallbackService:
    from cloud_platform.db.session import SessionFactory

    return TetraminatorCallbackService(
        payments_repo=SqlAlchemyPaymentSessionRepository(SessionFactory),
        webhook_service=webhook,
        gateway=gateway,
    )


@router.get("/payments/tetraminator")
async def tetraminator_callback(
    ref: Annotated[str, Query(min_length=1, max_length=64)],
    service: Annotated[TetraminatorCallbackService, Depends(get_tetraminator_callback_service)],
) -> dict[str, str]:
    """Handle one Tetraminator GET callback (never credits blindly)."""
    try:
        outcome = await service.process_callback(ref=ref)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if outcome.session is None:
        raise HTTPException(status_code=404, detail="unknown payment")
    return {"action": outcome.action.value, "session_status": outcome.session.status.value}
