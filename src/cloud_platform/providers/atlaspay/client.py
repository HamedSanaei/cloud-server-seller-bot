"""AtlasPay card-to-card orders; never store or expose card data.

The invoice uses integer Toman (IRT minor units). AtlasPay may append a
unique payment suffix: ``totalAmountToman`` is the amount to PAY and verify,
while the wallet receives only the customer's frozen requested base amount.
"""

from __future__ import annotations

import re
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


def _positive_integer(raw: Any, field: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise ProviderError(f"atlaspay {field} must be a positive integer")
    return raw


def _tracking_code(raw: Any) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw):
        raise ProviderError("atlaspay returned an invalid tracking code")
    return raw


class AtlasPayGateway(CapabilityGatedGateway):
    """HTTPS-only order creation and authoritative read-only status inquiry."""

    key = "atlaspay"
    supported_currency = "IRT"
    wallet_currency_only = True
    requires_telegram_customer = True
    final_amount_from_gateway = True
    capabilities = frozenset({GatewayCapability.CREATE_PAYMENT, GatewayCapability.VERIFY_PAYMENT})

    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout_seconds: float = 20.0,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("atlaspay API key is required")
        base = base_url.rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("atlaspay base URL must be an absolute HTTPS URL")
        self._base_url = base
        self._headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, f"{self._base_url}{path}", headers=self._headers, json=body
            )
        except httpx.RequestError as exc:
            raise ProviderUnavailable("atlaspay request unavailable") from exc
        status = response.status_code
        if status in (401, 403):
            raise ProviderAuthError("atlaspay authentication failed")
        if status == 404:
            raise ProviderNotFound("atlaspay order not found")
        if status == 429:
            raise ProviderRateLimited("atlaspay rate limited")
        if status >= 500:
            raise ProviderUnavailable(f"atlaspay unavailable (HTTP {status})")
        if response.is_error:
            raise ProviderError(f"atlaspay rejected the request (HTTP {status})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("atlaspay returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise ProviderError("atlaspay rejected the operation")
        return payload

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
    ) -> PaymentIntent:
        self._require_capability(GatewayCapability.CREATE_PAYMENT)
        if (
            currency != "IRT"
            or isinstance(amount_minor, bool)
            or not isinstance(amount_minor, int)
            or amount_minor <= 0
        ):
            raise ValueError("atlaspay requires a positive integer Toman amount in IRT")
        try:
            telegram_id = int(reference)
        except (ValueError, TypeError) as exc:
            raise ValueError("atlaspay requires a numeric customer Telegram id") from exc
        if telegram_id <= 0 or str(telegram_id) != reference:
            raise ValueError("atlaspay requires a positive customer Telegram id")
        del redirect_url  # Hosted miniapp supplies its own link; no callback URL.
        payload = await self._request(
            "POST",
            "/orders",
            body={
                "merchantOrderRef": idempotency_key.value,
                "baseAmountToman": amount_minor,
                "customerTelegramId": telegram_id,
            },
        )
        order_id = _positive_integer(payload.get("orderId"), "orderId")
        total = _positive_integer(payload.get("totalAmountToman"), "totalAmountToman")
        if total < amount_minor:
            raise ProviderError("atlaspay returned a total smaller than the requested amount")
        tracking = _tracking_code(payload.get("trackingCode"))
        link = payload.get("customerStartLink")
        if not isinstance(link, str) or not link.startswith("https://t.me/"):
            raise ProviderError("atlaspay returned no HTTPS miniapp link")
        return PaymentIntent(
            gateway_payment_id=str(order_id),
            status=PaymentStatus.PENDING,
            amount_minor=total,
            currency="IRT",
            redirect_url=link,
            metadata={"tracking_code": tracking, "merchant_order_ref": idempotency_key.value},
        )

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        """GET status is authoritative; paid requires full amount, no manual delivery."""
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        try:
            order_id = int(gateway_payment_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("atlaspay order id must be numeric") from exc
        if order_id <= 0 or str(order_id) != gateway_payment_id:
            raise ValueError("atlaspay order id must be a positive integer")
        payload = await self._request("GET", f"/orders/{order_id}")
        if payload.get("success") is not True:
            raise ProviderError("atlaspay inquiry was not successful")
        returned_id = _positive_integer(payload.get("id"), "id")
        if returned_id != order_id:
            raise ProviderError("atlaspay inquiry order id mismatch")
        amount = _positive_integer(payload.get("totalAmountToman"), "totalAmountToman")
        merchant_ref = payload.get("merchantOrderRef")
        if not isinstance(merchant_ref, str) or not merchant_ref.strip():
            raise ProviderError("atlaspay inquiry has no merchant reference")
        tracking = _tracking_code(payload.get("trackingCode"))
        manual = payload.get("requiresManualDelivery")
        if not isinstance(manual, bool):
            raise ProviderError("atlaspay inquiry has no delivery decision")
        state = payload.get("status")
        if state in ("confirmed", "settled"):
            received = payload.get("actualReceivedAmountToman")
            if isinstance(received, bool) or not isinstance(received, int) or received <= 0:
                raise ProviderError("atlaspay confirmed order has no actual received amount")
            if manual is False:
                if received != amount:
                    raise ProviderError("atlaspay received amount differs from the full order")
                status = PaymentStatus.SUCCEEDED
            else:
                status = PaymentStatus.PENDING
        elif state in ("rejected", "expired", "cancelled"):
            status = PaymentStatus.FAILED
        elif state in (
            "awaiting_payment",
            "admin_review",
            "underpaid_review",
            "underpaid_awaiting_remainder",
        ):
            status = PaymentStatus.PENDING
        else:
            raise ProviderError("atlaspay returned an unknown order status")
        return PaymentIntent(
            gateway_payment_id=str(returned_id),
            status=status,
            amount_minor=amount,
            currency="IRT",
            metadata={
                "merchant_order_ref": merchant_ref,
                "tracking_code": tracking,
                "manual_delivery": "true" if manual else "false",
                "provider_status": state,
            },
        )
