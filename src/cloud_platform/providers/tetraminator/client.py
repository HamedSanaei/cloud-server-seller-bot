"""Tetraminator payment gateway adapter.

Second Iranian payment gateway. Implements the provider-neutral
``PaymentGateway`` port (``CapabilityGatedGateway``) against the
Tetraminator seller API (see ``docs/payments/TETRAMINATOR.md``):

- ``POST {base}/invoice/create`` — body ``{price, callback_url}``;
  ``201 {status, message, pay_id, payment_link}``.
- ``GET {base}/payment/inquiry/{pay_id}`` — ``{status, payment_status,
  pay_id, amount}``. Credit requires ``status is true`` AND
  ``payment_status == "paid"`` (checked by the caller against the stored
  session — this adapter never credits anything itself).
- Auth: ``X-API-KEY`` header on every call. No documented webhook
  signature exists, so the GET callback is untrusted by design and every
  credit goes through inquiry verification first.

Money boundary (audited, tested in one place): the external ``price``/
``amount`` fields are integer TOMAN. Internally the platform keeps integer
minor units and treats IRT minor units AS Toman (zero-decimal currency),
so ``internal amount_minor == Tetraminator price`` exactly — no float, no
division, no FX. Only ``IRT`` wallets are supported; EUR (or anything
else) is refused rather than converted.

Safety: the minimum documented charge (50,000 Toman) is enforced
server-side here as well as in the UI. Errors map onto the platform
hierarchy; the API key never appears in errors, logs, metric labels or
reprs.
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

GATEWAY_KEY = "tetraminator"

#: The only wallet currency this gateway can charge (Toman minor units).
CURRENCY = "IRT"

PRODUCTION_BASE_URL = "https://api.tetraminator.com/v1"

#: Documented minimum invoice amount, in Toman (= IRT minor units).
MINIMUM_TOMAN = 50_000


def toman_price_for(amount_minor: int, currency: str) -> int:
    """Internal wallet amount -> Tetraminator ``price`` (integer Toman).

    This is the ONE tested conversion boundary: identity for IRT, refused
    for anything else (no implicit Rial/Toman division, no FX).
    """
    if currency != CURRENCY:
        raise ValueError(f"tetraminator only supports {CURRENCY} (got {currency})")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError("amount must be a positive integer of minor units")
    return amount_minor


class TetraminatorGateway(CapabilityGatedGateway):
    """Tetraminator adapter (create invoice + inquiry; no refund endpoint)."""

    key = GATEWAY_KEY
    capabilities = frozenset({GatewayCapability.CREATE_PAYMENT, GatewayCapability.VERIFY_PAYMENT})

    #: OUR callback contract (not Tetraminator's): the documented callback
    #: carries no body and no signature, so the callback URL we send MUST
    #: reference a durable local session (``?ref=<session-id>``). Setting this
    #: flag makes :class:`WalletRechargeService` persist the PENDING intent
    #: BEFORE the invoice call, so the reference exists and is stable.
    requires_callback_reference = True

    def __init__(
        self,
        api_key: str,
        base_url: str = PRODUCTION_BASE_URL,
        callback_url: str = "",
        timeout_seconds: float = 30.0,
        require_https_callback: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._callback_url = callback_url
        self._require_https_callback = require_https_callback
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))

    @property
    def supported_currency(self) -> str:
        """The only currency this gateway can charge in (see ``CURRENCY``)."""
        return CURRENCY

    @property
    def minimum_charge_minor(self) -> int:
        """Documented minimum invoice amount, in gateway minor units."""
        return MINIMUM_TOMAN

    @property
    def callback_url(self) -> str:
        """Configured callback base URL (no secrets in it)."""
        return self._callback_url

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "X-API-KEY": self._api_key}

    def _checked_callback_url(self, callback_url: str) -> str:
        callback = (callback_url or self._callback_url).strip()
        if not callback:
            raise ValueError("callback URL is required (redirect_url or configured callback_url)")
        lowered = callback.lower()
        if not (lowered.startswith("https://") or lowered.startswith("http://")):
            raise ValueError("callback URL must begin with http:// or https://")
        if self._require_https_callback and not lowered.startswith("https://"):
            raise ValueError("production callback URL must use https://")
        return callback

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
        price = toman_price_for(amount_minor, currency)
        if price < MINIMUM_TOMAN:
            raise ValueError(
                f"amount {price} is below the Tetraminator minimum of {MINIMUM_TOMAN} Toman"
            )
        if not reference or not reference.strip():
            raise ValueError("reference must not be empty")
        callback = self._checked_callback_url(redirect_url or "")
        payload = await self._post("/invoice/create", {"price": price, "callback_url": callback})
        if not isinstance(payload, dict) or payload.get("status") is not True:
            raise ProviderError("tetraminator invoice creation was not accepted")
        pay_id = str(payload.get("pay_id") or "").strip()
        payment_link = str(payload.get("payment_link") or "").strip()
        if not pay_id:
            raise ProviderError("tetraminator invoice response carries no pay_id")
        if not payment_link:
            raise ProviderError("tetraminator invoice response carries no payment_link")
        return PaymentIntent(
            gateway_payment_id=pay_id,
            status=PaymentStatus.PENDING,
            amount_minor=amount_minor,
            currency=currency,
            redirect_url=payment_link,
            metadata={"reference": reference},
        )

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        """Read-only inquiry for one ``pay_id`` (no amount needed).

        Maps the documented response onto a PaymentIntent: SUCCEEDED only
        when ``status is true`` AND ``payment_status`` is ``"paid"``;
        anything else stays PENDING (never auto-failed — unknown provider
        states must not destroy a pending session). The caller compares
        pay_id and the exact amount against the stored session before any
        credit.
        """
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        pay_id = (gateway_payment_id or "").strip()
        if not pay_id:
            raise ValueError("gateway_payment_id (pay_id) must not be empty")
        payload = await self._get(f"/payment/inquiry/{pay_id}")
        if not isinstance(payload, dict):
            raise ProviderError("tetraminator inquiry returned an unusable payload")
        ok = payload.get("status") is True
        state = str(payload.get("payment_status") or "").strip().lower()
        returned_id = str(payload.get("pay_id") or "").strip()
        amount = payload.get("amount")
        amount_minor = amount if isinstance(amount, int) and not isinstance(amount, bool) else 0
        if ok and state == "paid" and returned_id:
            return PaymentIntent(
                gateway_payment_id=returned_id,
                status=PaymentStatus.SUCCEEDED,
                amount_minor=amount_minor,
                currency=CURRENCY,
                redirect_url=None,
                metadata={},
            )
        return PaymentIntent(
            gateway_payment_id=returned_id or pay_id,
            status=PaymentStatus.PENDING,
            amount_minor=amount_minor,
            currency=CURRENCY,
            redirect_url=None,
            metadata={"payment_status": state},
        )

    def payment_link_for(self, pay_id: str) -> str:
        """Reconstruct the documented payment link shape for a ``pay_id``.

        Used only to re-show an already-issued link on idempotent replay
        (the issued link itself always comes from the create response).
        """
        cleaned = (pay_id or "").strip()
        if not cleaned:
            raise ValueError("pay_id must not be empty")
        return f"https://t.me/tetraminator_bot?start=pay_{cleaned}"

    async def refund(
        self,
        *,
        gateway_payment_id: str,
        amount_minor: int,
        idempotency_key: IdempotencyKey,
        reason: str = "",
    ) -> GatewayRefund:
        self._require_capability(GatewayCapability.REFUND)
        raise NotImplementedError("tetraminator exposes no refund endpoint")  # pragma: no cover

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        async with metrics.provider_call(self.key, f"POST {path}"):
            try:
                response = await self._client.post(
                    f"{self._base_url}{path}", json=body, headers=self._headers()
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailable(str(exc)) from exc
        return self._checked_payload(response, f"POST {path}")

    async def _get(self, path: str) -> Any:
        async with metrics.provider_call(self.key, f"GET {path}"):
            try:
                response = await self._client.get(
                    f"{self._base_url}{path}", headers=self._headers()
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailable(str(exc)) from exc
        return self._checked_payload(response, f"GET {path}")

    def _checked_payload(self, response: httpx.Response, operation: str) -> Any:
        status = response.status_code
        if status == 401:
            raise ProviderAuthError("tetraminator rejected the API key")
        if status == 403:
            raise ProviderError("tetraminator seller/store is disabled")
        if status == 404:
            raise ProviderNotFound("tetraminator endpoint not found")
        if status == 429:
            raise ProviderRateLimited("tetraminator rate limit exceeded")
        if status >= 500:
            raise ProviderUnavailable(f"tetraminator unavailable (HTTP {status})")
        if status == 400:
            raise ProviderError("tetraminator rejected the request (bad data or amount)")
        if response.is_error:
            raise ProviderError(f"tetraminator request failed (HTTP {status})")
        try:
            return response.json()
        except Exception as exc:
            raise ProviderError("tetraminator returned non-JSON payload") from exc
