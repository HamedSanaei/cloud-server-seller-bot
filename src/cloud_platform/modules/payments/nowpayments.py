"""Native signed IPNs only identify existing durable reconciliation work."""

from typing import Any

from cloud_platform.modules.payments.inquiry import AuthoritativePaymentService
from cloud_platform.providers.errors import ProviderError
from cloud_platform.providers.nowpayments.client import NowPaymentsGateway


class NowPaymentsCallbackService:
    def __init__(
        self, *, payments_repo: Any, authoritative_service: AuthoritativePaymentService
    ) -> None:
        self._payments = payments_repo
        self._authoritative = authoritative_service

    async def identify_session(self, payload: dict[str, Any]) -> Any:
        """Use signed identity hints only; neither create nor bind from payload."""
        try:
            invoice_id = NowPaymentsGateway._invoice_id(payload.get("invoice_id"))
        except ProviderError:
            return None
        reference = payload.get("order_id")
        if not isinstance(reference, str):
            return None
        key = NowPaymentsGateway._merchant_ref_for(reference)
        if not key or reference != NowPaymentsGateway.provider_order_ref_for(key):
            return None
        session = await self._payments.get_by_idempotency_key("nowpayments", key)
        if session is None or session.gateway_key != "nowpayments":
            return None
        if session.gateway_payment_id not in {None, invoice_id}:
            return None
        return session

    async def process_callback(self, payload: dict[str, Any]) -> str:
        session = await self.identify_session(payload)
        if session is None:
            return "ignored"
        try:
            await self._authoritative.reconcile_session(session)
        except Exception:
            # The pre-existing durable row remains in the bounded worker scan,
            # including ambiguous unbound attempts. Never trust IPN status.
            return "pending"
        return "reconciled"
