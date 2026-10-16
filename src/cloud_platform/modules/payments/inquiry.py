"""Provider-neutral authoritative inquiry shared by polling and customer checks."""

from __future__ import annotations

from dataclasses import replace
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
        type(intent.amount_minor) is not int
        or intent.gateway_payment_id != session.gateway_payment_id
        or intent.amount_minor != session.amount_minor
        or intent.currency != session.currency
    ):
        raise ProviderError("verified payment does not match the durable invoice")
    saved_tracking = (session.payment_details or {}).get("tracking_code") or session.tracking_code
    verified_tracking = intent.metadata.get("tracking_code")
    if saved_tracking and saved_tracking != verified_tracking:
        raise ProviderError("verified tracking code does not match the durable invoice")
    merchant_reference = intent.metadata.get("merchant_order_ref")
    if merchant_reference is not None and merchant_reference != session.idempotency_key:
        raise ProviderError("verified merchant reference does not match the durable invoice")
    return intent


class AuthoritativePaymentService:
    """One provider verification and wallet settlement path for every trigger."""

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

    async def inspect_session(self, session: PaymentSession) -> PaymentIntent:
        """Read-only verification, including a held invoice; never releases a hold."""
        gateway = self._gateways.get(session.gateway_key)
        if gateway is None:
            raise ProviderError("payment gateway is unavailable for inquiry")
        return await verify_payment_session(gateway, session)

    async def reconcile_session(self, session: PaymentSession) -> PaymentSession:
        if session.status is PaymentSessionStatus.SUCCEEDED:
            if session.credited_at is None:
                outcome = await self._webhook.process_callback(
                    gateway_key=session.gateway_key,
                    external_id=session.gateway_payment_id or "",
                    status="succeeded",
                )
                return outcome.session
            return session
        if session.status is not PaymentSessionStatus.PENDING:
            return session
        gateway = self._gateways.get(session.gateway_key)
        if gateway is None:
            raise ProviderError("payment gateway is unavailable for inquiry")
        if not session.gateway_payment_id:
            recover = getattr(gateway, "recover_payment", None)
            if not callable(recover):
                return session
            recovered = await recover(
                amount_minor=session.amount_minor, merchant_order_ref=session.idempotency_key
            )
            if recovered is None:
                return session
            # Recovery must prove all immutable local facts before first binding.
            if (
                recovered.amount_minor != session.amount_minor
                or recovered.currency != session.currency
                or recovered.metadata.get("merchant_order_ref") != session.idempotency_key
            ):
                raise ProviderError("recovered payment does not match the durable attempt")
            session = await self._payments.save(
                replace(
                    session.with_payment_intent(
                        recovered.gateway_payment_id, recovered.amount_minor, recovered.metadata
                    ),
                    redirect_url=recovered.redirect_url,
                )
            )
        intent = await verify_payment_session(gateway, session)
        if session.gateway_key == "nowpayments":
            concrete_id = intent.metadata.get("payment_id")
            if concrete_id:
                session = await self._payments.save(session.bind_provider_payment(concrete_id))
            elif intent.status is PaymentStatus.SUCCEEDED:
                raise ProviderError("verified payment omitted concrete payment identity")
        if intent.status in {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED}:
            outcome = await self._webhook.process_callback(
                gateway_key=session.gateway_key,
                external_id=intent.gateway_payment_id,
                status=intent.status.value,
            )
            return outcome.session
        return session


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
        self._authoritative = AuthoritativePaymentService(
            payments_repo=payments_repo, webhook_service=webhook_service, gateways=gateways
        )

    async def check_status(self, user: object, session_id: UUID) -> PaymentSession:
        session = await self._payments.get(session_id)
        if session is None or session.user_id != getattr(user, "id", None):
            raise PermissionDeniedError("payment session belongs to another user")
        return await self._authoritative.reconcile_session(session)
