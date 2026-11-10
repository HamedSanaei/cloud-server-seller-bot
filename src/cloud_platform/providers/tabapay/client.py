"""TabaPay v1 hosted payments. All settlement amounts are integer Rial."""

from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING, Any
from uuid import UUID

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
    ProviderRateLimited,
    ProviderUnavailable,
)

if TYPE_CHECKING:
    from cloud_platform.core.config import Settings

ORIGIN = "https://api.tabapay.ir"
CURRENCY = "IRR"


def valid_token(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and str(parsed) == value.lower()


def integer(value: object) -> int | None:
    """TabaPay uses both JSON integers and decimal strings."""
    if type(value) is int:
        return value
    if isinstance(value, str) and 0 < len(value) <= 20:
        digits = value.removeprefix("-")
        if digits.isascii() and digits.isdecimal():
            return int(value)
    return None


class TabaPayGateway(CapabilityGatedGateway):
    key = "tabapay"
    supported_currency = CURRENCY
    minimum_charge_minor = 100_000
    maximum_charge_minor = 500_000_000
    requires_durable_creation = True
    capabilities = frozenset({GatewayCapability.CREATE_PAYMENT, GatewayCapability.VERIFY_PAYMENT})

    def __init__(
        self, *, merchant_id: str, callback_url: str, sandbox: bool, timeout_seconds: float
    ) -> None:
        if not valid_token(merchant_id):
            raise ValueError("invalid TabaPay merchant ID")
        self._merchant_id = merchant_id
        self._callback_url = callback_url
        self._prefix = "/sandbox" if sandbox else ""
        self._client = httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    async def close(self) -> None:
        await self._client.aclose()

    @classmethod
    def from_settings(cls, settings: Settings) -> TabaPayGateway:
        return cls(
            merchant_id=settings.tabapay_merchant_id,
            callback_url=settings.tabapay_callback_url,
            sandbox=settings.tabapay_sandbox,
            timeout_seconds=settings.tabapay_timeout_seconds,
        )

    def _validate_amount(self, amount_minor: int, currency: str) -> None:
        if currency != CURRENCY:
            raise ValueError("TabaPay charges in IRR")
        if (
            type(amount_minor) is not int
            or not self.minimum_charge_minor <= amount_minor <= self.maximum_charge_minor
        ):
            raise ValueError("TabaPay amount must be between 100,000 and 500,000,000 Rial")

    def payment_link_for(self, token: str) -> str:
        if not isinstance(token, str) or not valid_token(token):
            raise ProviderError("TabaPay returned an invalid payment token")
        return f"{ORIGIN}{self._prefix}/pay/{token}"

    def authentic_notification(self, signature: str) -> bool:
        # The issuer's protocol uses the hex MD5 of the merchant credential.
        expected = hashlib.md5(self._merchant_id.encode("ascii"), usedforsecurity=False).hexdigest()
        return (
            len(signature) == 32
            and signature.isascii()
            and hmac.compare_digest(signature.lower(), expected)
        )

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
    ) -> PaymentIntent:
        self._validate_amount(amount_minor, currency)
        # The callback origin is operator-owned, never supplied by the browser.
        del reference, idempotency_key, redirect_url
        payload = await self._post(
            "create", {"amount": str(amount_minor), "callbackURL": self._callback_url}
        )
        if integer(payload.get("responseCode")) != 1 or payload.get("status") != "success":
            raise ProviderError("TabaPay did not create the payment")
        token = payload.get("token")
        if not isinstance(token, str):
            raise ProviderError("TabaPay omitted the payment token")
        url = self.payment_link_for(token)
        if payload.get("url") != url:
            raise ProviderError("TabaPay returned an unexpected payment URL")
        return PaymentIntent(token, PaymentStatus.PENDING, amount_minor, CURRENCY, url)

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        raise ProviderError("TabaPay verification requires the original Rial amount")

    async def verify_with_amount(self, token: str, amount_minor: int) -> PaymentIntent:
        self._validate_amount(amount_minor, CURRENCY)
        self.payment_link_for(token)
        payload = await self._post("verify", {"token": token, "amount": str(amount_minor)})
        code = integer(payload.get("responseCode"))
        reported_token = payload.get("token")
        reported_amount = integer(payload.get("amount"))
        if (reported_token is not None and reported_token != token) or (
            "amount" in payload and reported_amount != amount_minor
        ):
            raise ProviderError("TabaPay verification does not match the stored payment")
        status = PaymentStatus.PENDING
        metadata: dict[str, str] = {}
        if reported_token == token and reported_amount == amount_minor:
            reference = payload.get("shaparakRefNumber")
            if (
                code in {1, 2}
                and payload.get("status") == "success"
                and isinstance(reference, str)
                and 0 < len(reference) <= 100
                and reference.isascii()
                and reference.isdecimal()
                and reference.strip("0")
            ):
                status = PaymentStatus.SUCCEEDED
                metadata["ref_id"] = reference
            elif code == -102 and payload.get("status") == "error":
                status = PaymentStatus.FAILED
        return PaymentIntent(token, status, amount_minor, CURRENCY, metadata=metadata)

    async def _post(self, operation: str, fields: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{ORIGIN}/v1{self._prefix}/{operation}",
                headers={"Authorization": f"Bearer {self._merchant_id}"},
                files={name: (None, value) for name, value in fields.items()},
            )
        except httpx.TransportError:
            raise ProviderUnavailable("TabaPay connection failed") from None
        if response.status_code in (401, 403):
            raise ProviderAuthError("TabaPay rejected the merchant credential")
        if response.status_code == 429:
            raise ProviderRateLimited("TabaPay rate limit exceeded")
        if response.status_code >= 500 or response.is_redirect:
            raise ProviderUnavailable("TabaPay is temporarily unavailable")
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("TabaPay returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ProviderError("TabaPay returned an invalid response")
        code = integer(payload.get("responseCode"))
        if code in {-10, -11, -27, -28, -29, -30}:
            raise ProviderAuthError(f"TabaPay rejected the merchant configuration (code {code})")
        if response.is_error and (code is None or code >= 0):
            raise ProviderError("TabaPay rejected the request")
        return payload
