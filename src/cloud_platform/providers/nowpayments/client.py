"""NOWPayments hosted-invoice adapter (create + verify only).

NOWPayments is a crypto payment gateway: ``POST /invoice`` creates a *hosted*
invoice page (``invoice_url``) where the customer picks the cryptocurrency and
pays. Crypto choice stays on the hosted page, so the invoice request omits
``pay_currency``; ``is_fixed_rate``/``is_fee_paid_by_user`` are pinned false.

The provider's ``POST /invoice`` response carries the **invoice id** (``id``),
never a concrete payment id — a payment id only exists after the customer
starts paying. Therefore:

- the platform stores the invoice id as the canonical
  ``PaymentIntent.gateway_payment_id`` and the real ``invoice_url``/``id`` in
  metadata; a concrete ``payment_id`` is reported separately, never invented;
- recovery reads ``GET /payment/`` (list, authenticated with ``x-api-key`` AND
  a Bearer JWT from ``POST /auth``) and matches BOTH the stored invoice id AND
  the namespaced ``order_id``; the concrete payment is then confirmed through
  ``GET /payment/{payment_id}``. An invoice id is never sent as a payment id.

Creation is not retried. A timeout or transport failure during ``POST
/invoice`` is a genuinely ambiguous billable mutation and raises
:class:`ProviderOutcomeUnknown`; the durable attempt row is reconciled by
READ-ONLY listing, never by a second POST.

No payout/admin/merchant mutation endpoints are exposed. Provider secrets,
response bodies, tokens and headers are never logged, re-raised or placed in
exception messages.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderUnavailable,
)

GATEWAY_KEY = "nowpayments"
SUPPORTED_CURRENCY = "USD"
PRICE_CURRENCY = "usd"
DEFAULT_BASE_URL = "https://api.nowpayments.io/v1"
ORDER_ID_NAMESPACE = "cloud-server-seller"

# Documented NOWPayments payment lifecycle, grouped by platform meaning.
_PENDING_STATES = frozenset({"waiting", "confirming", "confirmed", "sending", "partially_paid"})
_FINISHED_STATES = frozenset({"finished"})
_FAILED_STATES = frozenset({"failed", "expired"})
_REFUNDED_STATES = frozenset({"refunded"})

_CENT = Decimal("0.01")
_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)
_MAX_IDENTITY_LENGTH = 200

_PLACEHOLDER_MARKERS = (
    "changeme",
    "change-me",
    "change_me",
    "placeholder",
    "your-api",
    "your_api",
    "your-key",
    "your_key",
    "replace-me",
    "replace_me",
    "insert-",
    "dummy-",
    "xxxxxx",
    "todo",
)


def _require_credential(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not text or "<" in value or ">" in value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} must not be empty or a placeholder")
    lowered = text.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if lowered == marker or lowered.startswith(marker + "-"):
            raise ValueError(f"{label} must not be a placeholder value")
    return text


def _require_email(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("auth_email must be a string")
    text = value.strip()
    if not text or "@" not in text or not text.isascii():
        raise ValueError("auth_email must be a valid email address")
    for marker in _PLACEHOLDER_MARKERS:
        if marker in text.lower():
            raise ValueError("auth_email must not be a placeholder value")
    return text


def _https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and not any(ord(char) < 33 for char in value)
    )


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite JSON number {token!r}")


def _hosted_invoice_url(value: Any) -> bool:
    if not isinstance(value, str) or not _https_url(value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.hostname in {"nowpayments.io", "www.nowpayments.io"} and parsed.port in {
            None,
            443,
        }
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class _RawNumber:
    """A JSON number emitted verbatim, so no float ever touches money."""

    text: str


@dataclass(frozen=True, slots=True)
class _ScanResult:
    candidates: tuple[dict[str, Any], ...]
    complete: bool


def _cents_to_plain(minor: int) -> str:
    whole, remainder = divmod(minor, 100)
    if remainder == 0:
        return str(whole)
    return f"{whole}.{remainder:02d}"


def _serialize_json(value: Any) -> str:
    """Serialize a request body, emitting ``_RawNumber`` as a JSON number."""
    if isinstance(value, _RawNumber):
        return value.text
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(key, ensure_ascii=False)}:{_serialize_json(item)}"
                for key, item in value.items()
            )
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_serialize_json(item) for item in value) + "]"
    if isinstance(value, int):
        return str(value)
    raise ProviderError("nowpayments request body is not serializable")


def _as_decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderError(f"nowpayments {field} is not a number")
    if isinstance(value, Decimal):
        decimal_value = value
    elif isinstance(value, int):
        decimal_value = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ProviderError(f"nowpayments {field} is empty")
        try:
            decimal_value = Decimal(text)
        except InvalidOperation:
            raise ProviderError(f"nowpayments {field} is not a number") from None
    else:
        raise ProviderError(f"nowpayments {field} is not a number")
    if not decimal_value.is_finite():
        raise ProviderError(f"nowpayments {field} is not finite")
    return decimal_value


def _parse_price_minor(value: Any, field: str) -> int:
    """Parse a fiat amount to integer cents, rejecting anything unsafe."""
    decimal_value = _as_decimal(value, field)
    if decimal_value < 0:
        raise ProviderError(f"nowpayments {field} must not be negative")
    try:
        cents = decimal_value.quantize(_CENT)
    except InvalidOperation:
        raise ProviderError(f"nowpayments {field} is out of range") from None
    if cents != decimal_value:
        raise ProviderError(f"nowpayments {field} has more than cent precision")
    minor = int(cents.scaleb(2))
    if not _INT64_MIN <= minor <= _INT64_MAX:
        raise ProviderError(f"nowpayments {field} is out of range")
    return minor


def _identity_string(value: Any, field: str) -> str:
    if isinstance(value, bool):
        raise ProviderError(f"nowpayments {field} is not a valid identifier")
    if isinstance(value, int):
        if value <= 0:
            raise ProviderError(f"nowpayments {field} is not a valid identifier")
        return str(value)
    if isinstance(value, str):
        text = value
        if len(text) > _MAX_IDENTITY_LENGTH or not re.fullmatch(r"[1-9][0-9]*", text):
            raise ProviderError(f"nowpayments {field} is not a valid identifier")
        return text
    raise ProviderError(f"nowpayments {field} is not a valid identifier")


class NowPaymentsGateway(CapabilityGatedGateway):
    """NOWPayments hosted-invoice adapter (create + verify capabilities only)."""

    key = GATEWAY_KEY
    supported_currency = SUPPORTED_CURRENCY
    requires_durable_creation = True
    capabilities = frozenset({GatewayCapability.CREATE_PAYMENT, GatewayCapability.VERIFY_PAYMENT})

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        callback_url: str = "",
        *,
        auth_email: str | None = None,
        auth_password: str | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_scan_pages: int = 20,
        page_size: int = 100,
    ) -> None:
        credential = _require_credential(api_key, "api_key")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("base URL must be a non-empty HTTPS URL")
        parsed_base = urlsplit(base_url)
        if not _https_url(base_url) or parsed_base.query or parsed_base.fragment:
            raise ValueError("base URL must use HTTPS without credentials/query/fragment")
        if not isinstance(callback_url, str) or not _https_url(callback_url):
            raise ValueError("callback URL must be a non-empty HTTPS URL")
        if (auth_email is None) != (auth_password is None):
            raise ValueError("auth_email and auth_password must be provided together")
        if auth_email is not None:
            auth_email = _require_email(auth_email)
            auth_password = _require_credential(auth_password, "auth_password")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or not math.isfinite(timeout_seconds)
        ):
            raise ValueError("timeout_seconds must be a positive number")
        if (
            isinstance(max_scan_pages, bool)
            or not isinstance(max_scan_pages, int)
            or (max_scan_pages <= 0)
        ):
            raise ValueError("max_scan_pages must be a positive integer")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not (1 <= page_size <= 1000)
        ):
            raise ValueError("page_size must be an integer in 1..1000")

        self._base_url = base_url.rstrip("/")
        self._callback_url = callback_url
        self._auth_email = auth_email
        self._auth_password = auth_password
        self._max_scan_pages = max_scan_pages
        self._page_size = page_size
        self._jwt: str | None = None
        self._jwt_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            headers={"x-api-key": credential},
            timeout=httpx.Timeout(float(timeout_seconds)),
            transport=transport,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Identity helpers
    # ------------------------------------------------------------------
    @staticmethod
    def provider_order_ref_for(idempotency_key: str) -> str:
        """Namespaced provider ``order_id`` for a local idempotency key."""
        key = (idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency key is required")
        return f"{ORDER_ID_NAMESPACE}:{key}"

    @staticmethod
    def _merchant_ref_for(provider_order_ref: str) -> str:
        prefix = f"{ORDER_ID_NAMESPACE}:"
        if provider_order_ref.startswith(prefix):
            return provider_order_ref[len(prefix) :]
        return provider_order_ref

    @staticmethod
    def _idempotency_key_value(idempotency_key: IdempotencyKey) -> str:
        value = getattr(idempotency_key, "value", None)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("idempotency key is required")
        return value.strip()

    @staticmethod
    def _invoice_id(value: Any) -> str:
        return _identity_string(value, "invoice id")

    def _validate_amount(self, amount_minor: int, currency: str) -> None:
        if type(amount_minor) is not int or not 0 < amount_minor <= _INT64_MAX:
            raise ValueError("payment amount must be a positive integer within signed int64 bounds")
        super()._validate_amount(amount_minor, currency)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
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
        if currency != SUPPORTED_CURRENCY:
            raise ValueError("nowpayments only supports USD")
        if not isinstance(reference, str) or not reference.strip():
            raise ValueError("payment reference is required")
        key = self._idempotency_key_value(idempotency_key)
        provider_order_ref = self.provider_order_ref_for(key)
        body: dict[str, Any] = {
            "price_amount": _RawNumber(_cents_to_plain(amount_minor)),
            "price_currency": PRICE_CURRENCY,
            "order_id": provider_order_ref,
            "ipn_callback_url": self._callback_url,
            # Crypto selection happens on the hosted page: no pay_currency.
            "is_fixed_rate": False,
            "is_fee_paid_by_user": False,
        }
        del redirect_url  # only this project's own IPN callback is used
        response = await self._request(
            "POST",
            "/invoice",
            content=_serialize_json(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            ambiguous=True,
        )
        payload = self._checked_response(response)
        invoice_id = _identity_string(payload.get("id"), "invoice id")
        order_value = payload.get("order_id")
        if not isinstance(order_value, str) or order_value != provider_order_ref:
            raise ProviderError("nowpayments order reference mismatch")
        if _parse_price_minor(payload.get("price_amount"), "price_amount") != amount_minor:
            raise ProviderError("nowpayments invoice amount mismatch")
        if str(payload.get("price_currency") or "").strip().upper() != SUPPORTED_CURRENCY:
            raise ProviderError("nowpayments invoice currency mismatch")
        invoice_url = payload.get("invoice_url")
        if not _hosted_invoice_url(invoice_url):
            raise ProviderError("nowpayments invoice URL is invalid")
        metadata = {
            "merchant_order_ref": key,
            "provider_order_ref": provider_order_ref,
            "invoice_id": invoice_id,
            "invoice_url": invoice_url,
            "payment_url": invoice_url,
            "currency": SUPPORTED_CURRENCY,
        }
        return PaymentIntent(
            gateway_payment_id=invoice_id,
            status=PaymentStatus.PENDING,
            amount_minor=amount_minor,
            currency=SUPPORTED_CURRENCY,
            redirect_url=invoice_url,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------
    async def verify_with_reference(
        self, gateway_payment_id: str, amount_minor: int, merchant_order_ref: str
    ) -> PaymentIntent:
        """Verify the hosted invoice bound to a local idempotency key."""
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        invoice_id = self._invoice_id(gateway_payment_id)
        if type(amount_minor) is not int or not 0 < amount_minor <= _INT64_MAX:
            raise ValueError("amount must be a positive integer within signed int64 bounds")
        ref = (merchant_order_ref or "").strip()
        if not ref:
            raise ValueError("merchant_order_ref is required")
        provider_order_ref = self.provider_order_ref_for(ref)
        scan = await self._scan_by_order(provider_order_ref)
        if not scan.complete:
            raise ProviderUnavailable("nowpayments payment listing is incomplete")
        if len(scan.candidates) > 1:
            raise ProviderError(
                "nowpayments found multiple matching payments; manual review required"
            )
        if not scan.candidates:
            return self._pending_intent(
                invoice_id,
                amount_minor,
                merchant_order_ref=ref,
                provider_order_ref=provider_order_ref,
            )
        intent = await self._verify_concrete(
            scan.candidates[0], invoice_id=invoice_id, provider_order_ref=provider_order_ref
        )
        if intent.amount_minor != amount_minor:
            raise ProviderError("nowpayments payment amount mismatch")
        return intent

    async def verify_payment(self, gateway_payment_id: str) -> PaymentIntent:
        """Verify by invoice id alone: invoice id AND our namespaced order id.

        Hosted invoices have no concrete payment id until the customer pays, so
        when nothing is found yet the invoice is reported as pending with an
        unknown (zero) amount. Callers that know the local merchant reference
        should prefer :meth:`verify_with_reference`.
        """
        self._require_capability(GatewayCapability.VERIFY_PAYMENT)
        invoice_id = self._invoice_id(gateway_payment_id)
        scan = await self._scan_by_invoice(invoice_id)
        if not scan.complete:
            raise ProviderUnavailable("nowpayments payment listing is incomplete")
        if len(scan.candidates) > 1:
            raise ProviderError(
                "nowpayments found multiple matching payments; manual review required"
            )
        if not scan.candidates:
            return self._pending_intent(invoice_id, 0, merchant_order_ref="", provider_order_ref="")
        entry = scan.candidates[0]
        provider_order_ref = _entry_order(entry)
        return await self._verify_concrete(
            entry, invoice_id=invoice_id, provider_order_ref=provider_order_ref
        )

    async def get_payment_details(self, gateway_payment_id: str) -> dict[str, str]:
        """Safe invoice metadata; best-effort, never invents an identity.

        When a concrete payment exists it returns the authoritative amount,
        merchant reference and concrete ``payment_id``. Otherwise it returns the
        invoice-level facts the platform already knows (currency + invoice id),
        so durable replay keeps using the stored ``invoice_url``.
        """
        invoice_id = self._invoice_id(gateway_payment_id)
        details: dict[str, str] = {"currency": SUPPORTED_CURRENCY, "invoice_id": invoice_id}
        if not self._has_credentials():
            return details
        try:
            scan = await self._scan_by_invoice(invoice_id)
        except ProviderError:
            # Listing is a recovery aid, not authoritative for replaying the
            # stored invoice URL; a transient listing failure stays non-fatal.
            return details
        if not scan.complete:
            raise ProviderUnavailable("nowpayments payment listing is incomplete")
        if len(scan.candidates) > 1:
            raise ProviderError(
                "nowpayments found multiple matching payments; manual review required"
            )
        if not scan.candidates:
            return details
        entry = scan.candidates[0]
        provider_order_ref = _entry_order(entry)
        intent = await self._verify_concrete(
            entry, invoice_id=invoice_id, provider_order_ref=provider_order_ref
        )
        details.update(
            {
                "total_amount_minor": str(intent.amount_minor),
                "merchant_order_ref": intent.metadata.get("merchant_order_ref", ""),
                "payment_id": intent.metadata.get("payment_id", ""),
                "provider_status": intent.metadata.get("provider_status", ""),
            }
        )
        return details

    # ------------------------------------------------------------------
    # Recovery internals
    # ------------------------------------------------------------------
    async def recover_payment(
        self, *, amount_minor: int, merchant_order_ref: str
    ) -> PaymentIntent | None:
        """Recover an ambiguous create using reads only, never another invoice POST.

        An invoice without any concrete payment may be absent from this API;
        absence leaves the durable attempt unresolved instead of issuing again.
        """
        order_ref = self.provider_order_ref_for(merchant_order_ref)
        scan = await self._scan_by_order(order_ref)
        if not scan.complete:
            raise ProviderUnavailable("nowpayments payment listing is incomplete")
        if len(scan.candidates) > 1:
            raise ProviderError("nowpayments ambiguous recovery requires manual review")
        if not scan.candidates:
            return None
        entry = scan.candidates[0]
        invoice_id = _entry_invoice(entry)
        if not invoice_id:
            raise ProviderError("nowpayments recovery omitted invoice identity")
        intent = await self._verify_concrete(
            entry, invoice_id=invoice_id, provider_order_ref=order_ref
        )
        if intent.amount_minor != amount_minor:
            raise ProviderError("nowpayments recovery invoice amount mismatch")
        return intent

    async def _verify_concrete(
        self,
        entry: dict[str, Any],
        *,
        invoice_id: str,
        provider_order_ref: str,
    ) -> PaymentIntent:
        payment_id = _identity_string(entry.get("payment_id"), "payment id")
        listed_invoice = _entry_invoice(entry)
        if listed_invoice != invoice_id:
            raise ProviderError("nowpayments payment invoice identity mismatch")
        detail = await self._get_payment(payment_id)
        if _identity_string(detail.get("payment_id"), "payment id") != payment_id:
            raise ProviderError("nowpayments payment identity mismatch")
        detail_invoice = detail.get("invoice_id")
        if _identity_string(detail_invoice, "invoice id") != invoice_id:
            raise ProviderError("nowpayments payment invoice identity mismatch")
        if str(detail.get("order_id") or "") != provider_order_ref:
            raise ProviderError("nowpayments payment order reference mismatch")
        return self._intent_from_payment(
            detail, invoice_id=invoice_id, provider_order_ref=provider_order_ref
        )

    def _intent_from_payment(
        self,
        payment: dict[str, Any],
        *,
        invoice_id: str,
        provider_order_ref: str,
    ) -> PaymentIntent:
        status_raw = payment.get("payment_status")
        if not isinstance(status_raw, str) or not status_raw.strip():
            raise ProviderError("nowpayments payment status is missing")
        status_key = status_raw.strip().lower()
        price_minor = _parse_price_minor(payment.get("price_amount"), "price_amount")
        if str(payment.get("price_currency") or "").strip().upper() != SUPPORTED_CURRENCY:
            raise ProviderError("nowpayments payment currency mismatch")
        payment_id = _identity_string(payment.get("payment_id"), "payment id")
        if status_key in _FINISHED_STATES:
            self._require_full_payment(payment)
            mapped = PaymentStatus.SUCCEEDED
        elif status_key in _REFUNDED_STATES:
            mapped = PaymentStatus.REFUNDED
        elif status_key in _FAILED_STATES:
            mapped = PaymentStatus.FAILED
        else:
            # waiting/confirming/confirmed/sending/partially_paid/unknown
            mapped = PaymentStatus.PENDING
        return PaymentIntent(
            gateway_payment_id=invoice_id,
            status=mapped,
            amount_minor=price_minor,
            currency=SUPPORTED_CURRENCY,
            metadata={
                "merchant_order_ref": self._merchant_ref_for(provider_order_ref),
                "provider_order_ref": provider_order_ref,
                "invoice_id": invoice_id,
                "payment_id": payment_id,
                "provider_status": status_key,
                "currency": SUPPORTED_CURRENCY,
            },
        )

    @staticmethod
    def _require_full_payment(payment: dict[str, Any]) -> None:
        pay_amount = payment.get("pay_amount")
        actually_paid = payment.get("actually_paid")
        if pay_amount is None or actually_paid is None:
            raise ProviderError("nowpayments finished payment lacks full-payment evidence")
        amount = _as_decimal(pay_amount, "pay_amount")
        paid = _as_decimal(actually_paid, "actually_paid")
        if amount <= 0 or paid <= 0:
            raise ProviderError("nowpayments full-payment evidence must be positive")
        if paid < amount:
            raise ProviderError("nowpayments finished payment is not fully paid")

    def _pending_intent(
        self,
        invoice_id: str,
        amount_minor: int,
        *,
        merchant_order_ref: str,
        provider_order_ref: str,
    ) -> PaymentIntent:
        return PaymentIntent(
            gateway_payment_id=invoice_id,
            status=PaymentStatus.PENDING,
            amount_minor=amount_minor,
            currency=SUPPORTED_CURRENCY,
            metadata={
                "merchant_order_ref": merchant_order_ref,
                "provider_order_ref": provider_order_ref,
                "invoice_id": invoice_id,
                "provider_status": "",
                "currency": SUPPORTED_CURRENCY,
            },
        )

    async def _scan_by_order(self, provider_order_ref: str) -> _ScanResult:
        """Collect payments whose provider ``order_id`` is exactly ours."""

        def match(entry: dict[str, Any]) -> bool:
            return _entry_order(entry) == provider_order_ref

        return await self._scan(match)

    async def _scan_by_invoice(self, invoice_id: str) -> _ScanResult:
        """Collect namespaced payments carrying our stored invoice id."""

        def match(entry: dict[str, Any]) -> bool:
            order = _entry_order(entry)
            if not order.startswith(f"{ORDER_ID_NAMESPACE}:"):
                return False
            return _entry_invoice(entry) == invoice_id

        return await self._scan(match)

    async def _scan(self, match: Callable[[dict[str, Any]], bool]) -> _ScanResult:
        """Page bounded listings and collect entries matching the predicate."""
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for page in range(self._max_scan_pages):
            payload = await self._list_payments(page=page, limit=self._page_size)
            data = payload.get("data")
            if not isinstance(data, list):
                raise ProviderError("nowpayments payment listing is malformed")
            for entry in data:
                if not isinstance(entry, dict):
                    raise ProviderError("nowpayments payment listing entry is malformed")
                if match(entry):
                    payment_id = _identity_string(entry.get("payment_id"), "payment id")
                    if payment_id not in seen:
                        candidates.append(entry)
                        seen.add(payment_id)
            pages = payload.get("pagesCount", payload.get("pages"))
            if pages is not None:
                if type(pages) is not int or pages < 0:
                    raise ProviderError("nowpayments payment pagination is malformed")
                if page + 1 >= pages:
                    return _ScanResult(tuple(candidates), True)
            elif not data or len(data) < self._page_size:
                return _ScanResult(tuple(candidates), True)
        return _ScanResult(tuple(candidates), False)

    async def _get_payment(self, payment_id: str) -> dict[str, Any]:
        response = await self._request("GET", f"/payment/{payment_id}")
        return self._checked_response(response)

    # ------------------------------------------------------------------
    # Auth + HTTP
    # ------------------------------------------------------------------
    def _has_credentials(self) -> bool:
        return bool(self._auth_email) and bool(self._auth_password)

    async def _ensure_token(self) -> str:
        token = self._jwt
        if token is not None:
            return token
        if not self._has_credentials():
            raise ProviderError("nowpayments payment listing requires API credentials")
        async with self._jwt_lock:
            token = await self._authenticate() if self._jwt is None else self._jwt
            self._jwt = token
        return token

    async def _refresh_token(self) -> str:
        async with self._jwt_lock:
            if not self._has_credentials():
                raise ProviderAuthError("nowpayments rejected API credentials")
            token = await self._authenticate()
            self._jwt = token
            return token

    async def _authenticate(self) -> str:
        body = json.dumps({"email": self._auth_email, "password": self._auth_password}).encode(
            "utf-8"
        )
        response = await self._request(
            "POST",
            "/auth",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        payload = self._checked_response(response)
        token = payload.get("token")
        if not isinstance(token, str) or not token.strip():
            raise ProviderError("nowpayments auth response omitted a token")
        return token.strip()

    async def _list_payments(self, *, page: int, limit: int) -> dict[str, Any]:
        params = {"limit": limit, "page": page, "sortBy": "created_at", "orderBy": "desc"}
        response = await self._request("GET", "/payment/", params=params, bearer=True)
        if response.status_code in {401, 403}:
            # One forced refresh on expiry; a second failure is terminal.
            await self._refresh_token()
            response = await self._request("GET", "/payment/", params=params, bearer=True)
        return self._checked_response(response)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        bearer: bool = False,
        ambiguous: bool = False,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if bearer:
            request_headers["Authorization"] = f"Bearer {await self._ensure_token()}"
        try:
            return await self._client.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                content=content,
                headers=request_headers or None,
            )
        except httpx.TimeoutException:
            raise self._transport_error(ambiguous) from None
        except httpx.TransportError:
            raise self._transport_error(ambiguous) from None

    @staticmethod
    def _transport_error(ambiguous: bool) -> ProviderError:
        if ambiguous:
            return ProviderOutcomeUnknown("nowpayments request outcome is unknown")
        return ProviderUnavailable("nowpayments transport failed")

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = json.loads(
                response.content, parse_float=Decimal, parse_constant=_reject_constant
            )
        except (ValueError, TypeError):
            raise ProviderError("nowpayments returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ProviderError("nowpayments returned an unexpected JSON shape")
        return payload

    @classmethod
    def _checked_response(cls, response: httpx.Response) -> dict[str, Any]:
        if response.status_code in {401, 403}:
            raise ProviderAuthError("nowpayments rejected credentials")
        if response.status_code == 404:
            raise ProviderNotFound("nowpayments resource not found")
        if response.status_code == 429:
            raise ProviderRateLimited("nowpayments rate limit exceeded")
        if response.status_code >= 500:
            raise ProviderUnavailable("nowpayments service unavailable")
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"nowpayments request failed (HTTP {response.status_code})")
        return cls._json_object(response)


def _entry_order(entry: dict[str, Any]) -> str:
    value = entry.get("order_id")
    return value if isinstance(value, str) else ""


def _entry_invoice(entry: dict[str, Any]) -> str | None:
    value = entry.get("invoice_id")
    if value is None:
        return None
    try:
        return _identity_string(value, "invoice id")
    except ProviderError:
        return ""


__all__ = [
    "DEFAULT_BASE_URL",
    "GATEWAY_KEY",
    "ORDER_ID_NAMESPACE",
    "PRICE_CURRENCY",
    "SUPPORTED_CURRENCY",
    "NowPaymentsGateway",
]
