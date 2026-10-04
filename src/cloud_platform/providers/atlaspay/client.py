"""AtlasPay orders: official top-level payloads and authoritative verification.

Creation is NOT retried: merchantOrderRef identifies an order but AtlasPay
makes no provider-side idempotency guarantee. The recharge application must
persist the attempt before calling create_payment and resume bound orders by
inquiry, never by issuing another POST. TRX merchant balances do not represent
the customer's wallet credit; the adapter charges integer Toman (IRT).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import (
    CapabilityGatedGateway,
    GatewayCapability,
    PaymentIntent,
    PaymentStatus,
)
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)

BASE_URL = "https://api.atlaspay.space/api/v1"
_PENDING = frozenset(
    {"awaiting_payment", "admin_review", "underpaid_review", "underpaid_awaiting_remainder"}
)
_SUCCEEDED = frozenset({"confirmed", "settled"})
_FAILED = frozenset({"rejected", "expired", "cancelled"})


def _positive_integer(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value <= 0:
        raise ProviderError(f"AtlasPay response has invalid {field}")
    return value


def _https_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


class AtlasPayGateway(CapabilityGatedGateway):
    key = "atlaspay"
    supported_currency = "IRT"
    minimum_charge_minor = 50_000
    maximum_charge_minor = 2_000_000
    requires_durable_creation = True
    capabilities = frozenset({GatewayCapability.CREATE_PAYMENT, GatewayCapability.VERIFY_PAYMENT})

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout_seconds: float = 30.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("AtlasPay API key is required")
        if not _https_url(base_url) or urlsplit(base_url).query or urlsplit(base_url).fragment:
            raise ValueError("AtlasPay base URL must use HTTPS without credentials/query/fragment")
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"X-API-Key": api_key},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
        customer_telegram_id: int | None = None,
    ) -> PaymentIntent:
        self._require_capability(GatewayCapability.CREATE_PAYMENT)
        if currency != self.supported_currency or type(amount_minor) is not int:
            raise ValueError("AtlasPay requires integer Toman in IRT")
        if not self.minimum_charge_minor <= amount_minor <= self.maximum_charge_minor:
            raise ValueError("AtlasPay base amount must be 50000..2000000 Toman")
        if not reference or not reference.strip():
            raise ValueError("payment reference is required")
        # The durable local idempotency key, not the customer's reusable UUID,
        # is the unique merchant order reference. No fabricated idempotency header.
        body: dict[str, Any] = {
            "merchantOrderRef": idempotency_key.value,
            "baseAmountToman": amount_minor,
        }
        if customer_telegram_id is not None:
            if type(customer_telegram_id) is not int or customer_telegram_id <= 0:
                raise ValueError("customer Telegram id must be a positive integer")
            body["customerTelegramId"] = customer_telegram_id
        del redirect_url  # Polling mode does not require a customer callback URL.
        payload = await self._request("POST", "/orders", body)
        if payload.get("merchantOrderRef", idempotency_key.value) != idempotency_key.value:
            raise ProviderError("AtlasPay creation merchant reference mismatch")
        order_id = str(_positive_integer(payload, "orderId"))
        total = _positive_integer(payload, "totalAmountToman")
        if total < amount_minor:
            raise ProviderError("AtlasPay total amount is below the requested base amount")
        details = self._details(payload, require_complete=True)
        return PaymentIntent(
            gateway_payment_id=order_id,
            status=PaymentStatus.PENDING,
            amount_minor=total,
            currency=self.supported_currency,
            redirect_url=details["payment_url"],
            metadata={**details, "merchant_order_ref": idempotency_key.value},
        )

    async def get_payment_details(self, gateway_payment_id: str) -> dict[str, str]:
        """Recover only real API metadata; never synthesize a Telegram link."""
        order_id = self._order_id(gateway_payment_id)
        payload = await self._request("GET", f"/orders/{order_id}")
        self._inquiry(payload, order_id)
        details = self._details(payload, require_complete=False)
        merchant_ref = payload.get("merchantOrderRef")
        if isinstance(merchant_ref, str):
            details["merchant_order_ref"] = merchant_ref
        return details

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        return await self._verify(gateway_payment_id)

    async def verify_with_amount(self, gateway_payment_id: str, amount_minor: int) -> PaymentIntent:
        return await self._verify(gateway_payment_id, amount_minor=amount_minor)

    async def verify_with_reference(
        self, gateway_payment_id: str, amount_minor: int, merchant_order_ref: str
    ) -> PaymentIntent:
        return await self._verify(
            gateway_payment_id, amount_minor=amount_minor, merchant_order_ref=merchant_order_ref
        )

    async def _verify(
        self,
        gateway_payment_id: str,
        *,
        amount_minor: int | None = None,
        merchant_order_ref: str | None = None,
    ) -> PaymentIntent:
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        order_id = self._order_id(gateway_payment_id)
        payload = await self._request("POST", f"/orders/{order_id}/verify")
        self._inquiry(payload, order_id)
        if type(payload.get("paid")) is not bool:
            raise ProviderError("AtlasPay verify response omitted paid")
        total = _positive_integer(payload, "totalAmountToman")
        if amount_minor is not None and total != amount_minor:
            raise ProviderError("AtlasPay payment amount mismatch")
        if merchant_order_ref is not None and payload.get("merchantOrderRef") != merchant_order_ref:
            raise ProviderError("AtlasPay merchant order reference mismatch")
        state = payload["status"]
        received = payload.get("actualReceivedAmountToman")
        if received is not None and (type(received) is not int or received < 0):
            raise ProviderError("AtlasPay received amount is invalid")
        if (
            state in _SUCCEEDED
            and payload["paid"]
            and not payload["requiresManualDelivery"]
            and received is not None
            and received < total
        ):
            raise ProviderError("AtlasPay full-payment evidence conflicts with received amount")
        if state in _FAILED:
            status = PaymentStatus.FAILED
        elif state in _SUCCEEDED and payload["paid"]:
            # Underpaid acceptance is not automatic settlement. Keep it pending
            # for a human decision, even though the provider calls it paid.
            status = (
                PaymentStatus.PENDING
                if payload["requiresManualDelivery"]
                else PaymentStatus.SUCCEEDED
            )
        else:
            status = PaymentStatus.PENDING
        return PaymentIntent(
            gateway_payment_id=order_id,
            status=status,
            amount_minor=total,
            currency=self.supported_currency,
            metadata={
                **self._details(payload, require_complete=False),
                "provider_status": state,
                "requires_manual_delivery": str(payload["requiresManualDelivery"]).lower(),
            },
        )

    @staticmethod
    def _order_id(value: str) -> str:
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.isdigit()
            or int(value) <= 0
        ):
            raise ValueError("AtlasPay payment id must be a positive integer")
        return str(int(value))

    @staticmethod
    def _inquiry(payload: dict[str, Any], order_id: str) -> None:
        if payload.get("success") is not True:
            raise ProviderError("AtlasPay inquiry did not succeed")
        if str(_positive_integer(payload, "id")) != order_id:
            raise ProviderError("AtlasPay order identity mismatch")
        _positive_integer(payload, "totalAmountToman")
        state = payload.get("status")
        if not isinstance(state, str) or state not in _PENDING | _SUCCEEDED | _FAILED:
            raise ProviderError("AtlasPay returned an unknown order state")
        if type(payload.get("requiresManualDelivery")) is not bool:
            raise ProviderError("AtlasPay inquiry omitted requiresManualDelivery")

    @staticmethod
    def _details(payload: dict[str, Any], *, require_complete: bool) -> dict[str, str]:
        details = {
            "total_amount_minor": str(_positive_integer(payload, "totalAmountToman")),
            "currency": "IRT",
        }
        for source, target in (
            ("trackingCode", "tracking_code"),
            ("paymentDeadlineAt", "payment_deadline_at"),
            ("customerStartLink", "payment_url"),
        ):
            value = payload.get(source)
            if not isinstance(value, str) or not value.strip():
                if require_complete:
                    raise ProviderError(f"AtlasPay response omitted {source}")
                continue
            if source == "trackingCode" and (
                not value.isascii() or not value.isalnum() or len(value) < 8
            ):
                raise ProviderError("AtlasPay tracking code is invalid")
            if source == "customerStartLink" and not _https_url(value):
                raise ProviderError("AtlasPay customer link must use HTTPS")
            if source == "paymentDeadlineAt":
                try:
                    deadline = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ProviderError("AtlasPay payment deadline is invalid") from exc
                if deadline.tzinfo is None:
                    raise ProviderError("AtlasPay payment deadline must include timezone")
            details[target] = value
        # Full destination card number, account balances, and raw response data
        # deliberately never cross this customer-metadata boundary.
        return details

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(method, f"{self._base_url}{path}", json=body)
        except httpx.TransportError as exc:
            raise ProviderUnavailable("AtlasPay transport failed") from exc
        if response.status_code in {401, 403}:
            raise ProviderAuthError("AtlasPay rejected credentials")
        if response.status_code == 404:
            raise ProviderNotFound("AtlasPay order or endpoint not found")
        if response.status_code == 429:
            raise ProviderRateLimited("AtlasPay rate limit exceeded")
        if response.status_code >= 500:
            raise ProviderUnavailable("AtlasPay service unavailable")
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"AtlasPay request failed (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("AtlasPay returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise ProviderError("AtlasPay returned an unsuccessful response")
        return payload
