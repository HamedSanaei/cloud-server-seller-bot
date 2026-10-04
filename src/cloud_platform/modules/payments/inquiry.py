"""Provider-neutral authoritative inquiry shared by polling and customer checks."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from cloud_platform.modules.payments.domain import (
    PaymentSession,
    PaymentSessionRepository,
    PaymentSessionStatus,
)
from cloud_platform.modules.payments.service import PaymentWebhookService
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.providers.base import PaymentIntent, PaymentStatus
from cloud_platform.providers.errors import ProviderError


async def verify_payment_session(gateway: Any, session: PaymentSession) -> PaymentIntent:
    """Verify identity, immutable merchant reference, currency and exact invoice total."""
    if gateway.key != session.gateway_key or not session.gateway_payment_id:
        raise ProviderError("payment gateway identity mismatch")
    intent: PaymentIntent
    reference_verifier = getattr(gateway, "verify_with_reference", None)
    if callable(reference_verifier):
        intent = await reference_verifier(
            session.gateway_payment_id, session.amount_minor, session.idempotency_key
        )
    else:
        amount_verifier = getattr(gateway, "verify_with_amount", None)
        if callable(amount_verifier):
            intent = await amount_verifier(session.gateway_payment_id, session.amount_minor)
        else:
            intent = await gateway.verify_payment(session.gateway_payment_id)
    if (
        intent.gateway_payment_id != session.gateway_payment_id
        or intent.amount_minor != session.amount_minor
        or intent.currency != session.currency
    ):
        raise ProviderError("verified payment does not match the durable invoice")
    saved_tracking = (session.payment_details or {}).get("tracking_code")
    verified_tracking = intent.metadata.get("tracking_code")
    if saved_tracking and verified_tracking and saved_tracking != verified_tracking:
        raise ProviderError("verified tracking code does not match the durable invoice")
    return intent


class PaymentInquiryService:
    """Authorized customer checks; disabled gateways still settle old invoices."""

    def __init__(
        self,
        *,
        payments_repo: PaymentSessionRepository,
        webhook_service: PaymentWebhookService,
        gateways: dict[str, Any],
    ) -> None:
        self._payments = payments_repo
        self._webhook = webhook_service
        self._gateways = gateways

    async def check_status(self, user: object, session_id: UUID) -> PaymentSession:
        session = await self._payments.get(session_id)
        if session is None or session.user_id != getattr(user, "id", None):
            raise PermissionDeniedError("payment session belongs to another user")
        if session.status is not PaymentSessionStatus.PENDING:
            return session
        gateway = self._gateways.get(session.gateway_key)
        if gateway is None:
            raise ProviderError("payment gateway is unavailable for inquiry")
        intent = await verify_payment_session(gateway, session)
        if intent.status in {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}:
            outcome = await self._webhook.process_callback(
                gateway_key=session.gateway_key,
                external_id=intent.gateway_payment_id,
                status=intent.status.value,
            )
            return outcome.session
        return session
