"""ZarinPal payment gateway adapter (M09-004).

First Iranian payment gateway. Implements the provider-neutral
``PaymentGateway`` port (``CapabilityGatedGateway``) against ZarinPal v4:

- ``POST {base}/request.json`` — create payment, returns ``authority`` +
  ``StartPay`` redirect URL. Amount is integer Rial (minor units), never float.
- ``POST {base}/verify.json`` — verify payment, returns ``ref_id`` on success.
- Sandbox: ``https://sandbox.zarinpal.com/pg/v4/payment`` + StartPay
  ``https://sandbox.zarinpal.com/pg/StartPay/{authority}``.
- Errors map onto the platform error hierarchy; the merchant id never
  appears in errors/logs/metric labels.

Sandbox/real test evidence: offline respx-style fake-HTTP suite in
``tests/unit/test_zarinpal_gateway.py`` covers the full request/verify
round-trip plus the failure table; live sandbox evidence is recorded by
running the same flow with ``ZARINPAL_SANDBOX=true`` and a sandbox merchant
id (see ``docs/payments/ZARINPAL.md``).
"""

from __future__ import annotations

from typing import Any

import httpx

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.base import (
    CapabilityGatedGateway,
    GatewayCapability,
    GatewayRefund,
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

GATEWAY_KEY = "zarinpal"
CURRENCY = "IRR"

PRODUCTION_BASE_URL = "https://api.zarinpal.com/pg/v4/payment"
SANDBOX_BASE_URL = "https://sandbox.zarinpal.com/pg/v4/payment"
PRODUCTION_STARTPAY_URL = "https://www.zarinpal.com/pg/StartPay"
SANDBOX_STARTPAY_URL = "https://sandbox.zarinpal.com/pg/StartPay"


class ZarinPalGateway(CapabilityGatedGateway):
    """ZarinPal v4 adapter (create + verify; no refund endpoint in v4)."""

    key = GATEWAY_KEY
    # Metadata is not a provider-side idempotency guarantee. Persist the
    # attempt before requesting an authority and never re-POST an ambiguous one.
    requires_durable_creation = True
    capabilities = frozenset({GatewayCapability.CREATE_PAYMENT, GatewayCapability.VERIFY_PAYMENT})

    def __init__(
        self,
        merchant_id: str,
        base_url: str = PRODUCTION_BASE_URL,
        sandbox: bool = False,
        callback_url: str = "",
        timeout_seconds: float = 30.0,
    ) -> None:
        if not merchant_id:
            raise ValueError("merchant_id must not be empty")
        self._merchant_id = merchant_id
        self._sandbox = sandbox
        self._base_url = (
            SANDBOX_BASE_URL if sandbox and base_url == PRODUCTION_BASE_URL else base_url
        ).rstrip("/")
        self._callback_url = callback_url
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    @property
    def supported_currency(self) -> str:
        """The only currency this gateway can charge in (see ``CURRENCY``)."""
        return CURRENCY

    @property
    def startpay_url(self) -> str:
        return SANDBOX_STARTPAY_URL if self._sandbox else PRODUCTION_STARTPAY_URL

    def payment_link_for(self, authority: str) -> str:
        return f"{self.startpay_url}/{authority}"

    async def close(self) -> None:
        await self._client.aclose()

    def _validate_amount(self, amount_minor: int, currency: str) -> None:
        if type(amount_minor) is not int or not 0 < amount_minor <= 2**63 - 1:
            raise ValueError("payment amount must be a positive integer within signed int64 bounds")
        super()._validate_amount(amount_minor, currency)

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
        self._validate_amount(amount_minor, currency)
        if currency != CURRENCY:
            raise ValueError(f"zarinpal only supports {CURRENCY} (got {currency})")
        if not reference or not reference.strip():
            raise ValueError("reference must not be empty")
        callback = redirect_url or self._callback_url
        if not callback:
            raise ValueError("callback URL is required (redirect_url or ZARINPAL_CALLBACK_URL)")
        body = {
            "merchant_id": self._merchant_id,
            "amount": amount_minor,
            "callback_url": callback,
            "description": f"wallet top-up {reference}",
            "metadata": {"reference": reference, "idempotency_key": idempotency_key.value},
        }
        payload = await self._post("/request.json", body)
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict) or type(data.get("code")) is not int:
            raise ProviderError("zarinpal request returned an invalid response")
        code = data.get("code")
        authority = str(data.get("authority") or "").strip()
        if code != 100 or not authority:
            raise ProviderError(f"zarinpal request failed (code={code})")
        return PaymentIntent(
            gateway_payment_id=authority,
            status=PaymentStatus.PENDING,
            amount_minor=amount_minor,
            currency=currency,
            redirect_url=self.payment_link_for(authority),
            metadata={"reference": reference},
        )

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        authority = (gateway_payment_id or "").strip()
        if not authority:
            raise ValueError("gateway_payment_id (authority) must not be empty")
        # Amount is required by ZarinPal verify; when unknown we verify with
        # a metadata-only probe is impossible, so callers must pass the
        # session amount via verify_with_amount. This method keeps the port
        # shape and raises a clear error directing to it.
        raise ProviderError(
            "verify_payment requires the original amount; "
            "use verify_with_amount(authority, amount_minor)"
        )

    async def verify_with_amount(self, authority: str, amount_minor: int) -> PaymentIntent:
        """Verify a ZarinPal authority for a known Rial amount."""
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        if not authority or not authority.strip():
            raise ValueError("authority must not be empty")
        self._validate_amount(amount_minor, CURRENCY)
        payload = await self._post(
            "/verify.json",
            {
                "merchant_id": self._merchant_id,
                "amount": amount_minor,
                "authority": authority.strip(),
            },
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict) or type(data.get("code")) is not int:
            raise ProviderError("zarinpal verify returned an invalid response")
        code = data.get("code")
        if code in (100, 101):
            ref_id = str(data.get("ref_id") or authority)
            return PaymentIntent(
                gateway_payment_id=authority.strip(),
                status=PaymentStatus.SUCCEEDED,
                amount_minor=amount_minor,
                currency=CURRENCY,
                redirect_url=None,
                metadata={"ref_id": ref_id},
            )
        if code in (-51, -53, -54):
            return PaymentIntent(
                gateway_payment_id=authority.strip(),
                status=PaymentStatus.FAILED,
                amount_minor=amount_minor,
                currency=CURRENCY,
                redirect_url=None,
                metadata={"code": str(code)},
            )
        raise ProviderError(f"zarinpal verify failed (code={code})")

    async def refund(
        self,
        *,
        gateway_payment_id: str,
        amount_minor: int,
        idempotency_key: IdempotencyKey,
        reason: str = "",
    ) -> GatewayRefund:
        self._require_capability(GatewayCapability.REFUND)
        raise NotImplementedError("zarinpal v4 exposes no refund endpoint")  # pragma: no cover

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        async with metrics.provider_call(self.key, f"POST {path}"):
            try:
                response = await self._client.post(f"{self._base_url}{path}", json=body)
            except httpx.TransportError:
                raise ProviderUnavailable("zarinpal transport failed") from None
        if response.status_code in (401, 403):
            raise ProviderAuthError("zarinpal rejected the merchant id")
        if response.status_code == 404:
            raise ProviderNotFound("zarinpal endpoint not found")
        if response.status_code == 429:
            raise ProviderRateLimited("zarinpal rate limit exceeded")
        if response.status_code >= 500:
            raise ProviderUnavailable(f"zarinpal unavailable (HTTP {response.status_code})")
        if response.is_error:
            raise ProviderError(f"zarinpal request failed (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise ProviderError("zarinpal returned non-JSON payload") from None
