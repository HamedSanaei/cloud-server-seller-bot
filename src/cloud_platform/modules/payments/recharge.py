"""Customer wallet top-up sessions (durable, replay-safe).

The recharge command is the missing half of the payment path: the webhook
service already turns a *successful* gateway callback into exactly one wallet
deposit, but nothing created the pending session. This service does, and it
emits the operator-channel ``recharge.created`` event — after the session is
durable, never before.

Safety properties:

- the gateway call happens FIRST and the session row is persisted with the
  returned authority; a crash between the two leaves an *unpaid* authority
  (harmless — no wallet effect without a verified callback);
- a replayed request (same Telegram button, same idempotency key) that the
  gateway resolves to the same authority collides on the session's unique
  ``(gateway_key, gateway_payment_id)`` pair and returns the EXISTING session
  instead of creating a second one;
- the amount is the customer's chosen integer minor units — the gateway
  adapter validates currency and amount, and no float ever touches money;
- a wallet is credited only by :class:`PaymentWebhookService` (verified
  callback + idempotent deposit key), never by this service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.businesslog.domain import BusinessEventSink, emit_safe
from cloud_platform.modules.businesslog.events import recharge_created_event
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    PaymentSession,
    PaymentSessionRepository,
    PaymentSessionStatus,
)

logger = logging.getLogger(__name__)


class RechargeError(Exception):
    """Base error for wallet top-up operations."""


class RechargeDisabledError(RechargeError):
    """No gateway is configured for the wallet's currency."""


class RechargeAmountError(RechargeError):
    """The requested top-up amount is not a positive integer amount."""


class RechargeGatewaySelectionRequired(RechargeError):
    """Several gateways fit; the caller must pick one explicitly."""

    def __init__(self, message: str, compatible: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.compatible = compatible


class RechargeGateway(Protocol):
    """The slice of the payment-gateway port this service needs."""

    key: str

    async def create_payment(
        self,
        *,
        amount_minor: int,
        currency: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        redirect_url: str | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class RechargeStart:
    """Outcome of ``start``: the durable session + where to send the user."""

    session: PaymentSession
    redirect_url: str
    replayed: bool


def _gateway_minimum_charge(gateway: Any, currency: str) -> int:
    """Gateway-enforced minimum charge in minor units (0 = none documented).

    Read defensively (getattr): single-currency adapters expose a plain
    ``minimum_charge_minor`` attribute; older adapters simply have none.
    """
    del currency
    raw = getattr(gateway, "minimum_charge_minor", 0)
    try:
        value = int(raw() if callable(raw) else raw)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _gateway_needs_callback_reference(gateway: Any) -> bool:
    """Whether the gateway needs our callback URL (with a local reference)
    at invoice-creation time. Gateways without this requirement keep the
    historical gateway-first flow untouched."""
    return bool(getattr(gateway, "requires_callback_reference", False))


def _gateway_callback_base(gateway: Any) -> str:
    return str(getattr(gateway, "callback_url", "") or "")


def _gateway_payment_link(gateway: Any, pay_id: str) -> str:
    """Reconstruct an already-issued payment link for idempotent replay."""
    builder = getattr(gateway, "payment_link_for", None)
    if not callable(builder):
        raise RechargeError("gateway cannot re-issue a payment link")
    link = builder(pay_id)
    if not link:
        raise RechargeError("gateway returned no payment link")
    return str(link)


class WalletRechargeService:
    """Creates pending gateway sessions for wallet top-ups."""

    def __init__(
        self,
        *,
        payments_repo: PaymentSessionRepository,
        gateway: RechargeGateway | None = None,
        gateways: dict[str, RechargeGateway] | None = None,
        event_sink: BusinessEventSink | None = None,
        user_repo: object | None = None,
    ) -> None:
        merged: dict[str, RechargeGateway] = dict(gateways or {})
        if gateway is not None:
            merged[gateway.key] = gateway
        self._gateways = merged
        self._payments = payments_repo
        self._events = event_sink
        self._users = user_repo

    @property
    def gateway_key(self) -> str:
        """Key of the single configured gateway (``""`` unless exactly one)."""
        if len(self._gateways) == 1:
            return next(iter(self._gateways))
        return ""

    @property
    def gateway_keys(self) -> tuple[str, ...]:
        """Keys of every configured gateway, in registration order."""
        return tuple(self._gateways)

    def _supports(self, gateway: RechargeGateway, currency: str) -> bool:
        supported = getattr(gateway, "supported_currency", "")
        return bool(supported) and str(supported).upper() == str(currency).upper()

    def supports_currency(self, currency: str) -> bool:
        """Whether any configured gateway can charge in ``currency``.

        Iranian gateways settle in their own currency, so a wallet in a
        different currency has no online top-up — the UI must say so instead
        of offering a button that cannot work.
        """
        return any(self._supports(gateway, currency) for gateway in self._gateways.values())

    def compatible_gateways(self, currency: str, amount_minor: int | None = None) -> list[str]:
        """Keys of gateways able to charge ``currency`` (and ``amount``).

        Drives the Telegram payment-method screen: zero means unavailable,
        one means the selection step is skipped, several means the customer
        picks. An amount below a gateway's documented minimum excludes it.
        """
        compatible: list[str] = []
        for key, gateway in self._gateways.items():
            if not self._supports(gateway, currency):
                continue
            if amount_minor is not None and amount_minor < _gateway_minimum_charge(
                gateway, currency
            ):
                continue
            compatible.append(key)
        return compatible

    def minimum_charge_minor(self, gateway_key: str, currency: str) -> int:
        """Gateway-enforced minimum charge (minor units) for UI gating."""
        gateway = self._gateways.get(gateway_key)
        if gateway is None or not self._supports(gateway, currency):
            return 0
        return _gateway_minimum_charge(gateway, currency)

    def _resolve_gateway(self, currency: str, gateway_key: str | None) -> RechargeGateway:
        if gateway_key is not None:
            gateway = self._gateways.get(gateway_key)
            if gateway is None or not self._supports(gateway, currency):
                raise RechargeDisabledError(f"gateway {gateway_key!r} cannot charge in {currency}")
            return gateway
        compatible = self.compatible_gateways(currency)
        if not compatible:
            raise RechargeDisabledError("no payment gateway is configured")
        if len(compatible) > 1:
            raise RechargeGatewaySelectionRequired(
                "several gateways fit; an explicit selection is required",
                tuple(compatible),
            )
        return self._gateways[compatible[0]]

    async def start(
        self,
        *,
        user: Any,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        redirect_url: str | None = None,
        gateway_key: str | None = None,
    ) -> RechargeStart:
        """Create (or replay) one pending top-up session."""
        if user is None or getattr(user, "id", None) is None:
            raise RechargeError("a persisted user id is required")
        if not isinstance(amount_minor, int) or amount_minor <= 0:
            raise RechargeAmountError("amount must be a positive integer of minor units")
        key = (idempotency_key or "").strip()
        if not key:
            raise RechargeError("idempotency_key must not be empty")
        if not 8 <= len(key) <= 128:
            # Cheap structural guard: fail here with a domain error instead of
            # leaking a ValueError out of the IdempotencyKey value object.
            raise RechargeError("idempotency_key must be 8..128 characters")
        gateway = self._resolve_gateway(currency, gateway_key)
        resolved_key = gateway.key
        minimum = _gateway_minimum_charge(gateway, currency)
        if amount_minor < minimum:
            raise RechargeAmountError(
                f"amount {amount_minor} is below the {resolved_key} minimum of {minimum}"
            )

        if _gateway_needs_callback_reference(gateway):
            return await self._start_with_callback_reference(
                gateway=gateway,
                user=user,
                amount_minor=amount_minor,
                currency=currency,
                key=key,
            )
        intent = await gateway.create_payment(
            amount_minor=amount_minor,
            currency=currency,
            reference=str(user.id),
            idempotency_key=IdempotencyKey(key),
            redirect_url=redirect_url,
        )
        authority = str(getattr(intent, "gateway_payment_id", "") or "")
        redirect = str(getattr(intent, "redirect_url", "") or "")
        if not authority:
            raise RechargeError("gateway returned no payment authority")

        session = PaymentSession(
            user_id=user.id,
            gateway_key=resolved_key,
            amount_minor=amount_minor,
            currency=currency,
            idempotency_key=key,
            gateway_payment_id=authority,
        )
        replayed = False
        try:
            session = await self._payments.create(session)
        except DuplicateExternalIdError:
            existing = await self._payments.get_by_external_id(resolved_key, authority)
            if existing is None:  # pragma: no cover - clash without a fetchable row
                raise
            if existing.user_id != user.id:
                raise RechargeError("this payment authority belongs to another user") from None
            session = existing
            replayed = True

        session_id = session.id
        if session_id is None:  # pragma: no cover - a persisted session always has an id
            raise RechargeError("payment session has no id")

        # Operator channel: enqueued only (delivery is a worker's job) and
        # keyed by the session id, so a replay cannot duplicate the message.
        await emit_safe(
            self._events,
            recharge_created_event(
                user=user,
                payment_session_id=session_id,
                amount_minor=session.amount_minor,
                currency=session.currency,
                gateway=session.gateway_key,
            ),
        )
        logger.info(
            "recharge session %s created for user %s (%d %s, gateway %s, replayed=%s)",
            session.id,
            user.id,
            amount_minor,
            currency,
            resolved_key,
            replayed,
        )
        return RechargeStart(session=session, redirect_url=redirect, replayed=replayed)

    async def _start_with_callback_reference(
        self,
        *,
        gateway: RechargeGateway,
        user: Any,
        amount_minor: int,
        currency: str,
        key: str,
    ) -> RechargeStart:
        """Start flow for gateways that need our callback URL at create time.

        The durable intent row is persisted FIRST (no external id yet), so
        the callback URL can reference the stable local session id. The
        gateway id is bound afterwards; a replay reuses an already-bound
        pending session instead of POSTing a second invoice. A concurrent
        double-tap that loses the initial insert race resumes the winner's
        row instead of failing.
        """
        resolved_key = gateway.key
        existing = await self._payments.get_by_idempotency_key(resolved_key, key)
        if existing is not None and existing.user_id != user.id:
            raise RechargeError("this recharge attempt belongs to another user") from None
        if (
            existing is not None
            and existing.status is PaymentSessionStatus.PENDING
            and existing.gateway_payment_id
        ):
            return RechargeStart(
                session=existing,
                redirect_url=_gateway_payment_link(gateway, existing.gateway_payment_id),
                replayed=True,
            )
        if existing is not None and existing.status is PaymentSessionStatus.PENDING:
            # Crashed between intent persist and gateway call: resume it.
            intent = existing
        else:
            try:
                intent = await self._payments.create(
                    PaymentSession(
                        user_id=user.id,
                        gateway_key=resolved_key,
                        amount_minor=amount_minor,
                        currency=currency,
                        idempotency_key=key,
                    )
                )
            except DuplicateExternalIdError:
                retry = await self._payments.get_by_idempotency_key(resolved_key, key)
                if retry is None:
                    raise
                intent = retry
                if intent.user_id != user.id:
                    raise RechargeError("this recharge attempt belongs to another user") from None
                if intent.status is PaymentSessionStatus.PENDING and intent.gateway_payment_id:
                    return RechargeStart(
                        session=intent,
                        redirect_url=_gateway_payment_link(gateway, intent.gateway_payment_id),
                        replayed=True,
                    )
        if intent.id is None:  # pragma: no cover - a persisted session always has an id
            raise RechargeError("payment session has no id")
        base = _gateway_callback_base(gateway)
        if not base:
            raise RechargeError(f"gateway {resolved_key!r} needs a callback URL but none is set")
        intent_result = await gateway.create_payment(
            amount_minor=amount_minor,
            currency=currency,
            reference=str(user.id),
            idempotency_key=IdempotencyKey(key),
            redirect_url=f"{base}?ref={intent.id}",
        )
        authority = str(getattr(intent_result, "gateway_payment_id", "") or "")
        redirect = str(getattr(intent_result, "redirect_url", "") or "")
        if not authority:
            raise RechargeError("gateway returned no payment authority")
        try:
            session = await self._payments.save(intent.with_gateway_payment_id(authority))
        except DuplicateExternalIdError:
            existing = await self._payments.get_by_external_id(resolved_key, authority)
            if existing is None:  # pragma: no cover - clash without a fetchable row
                raise
            if existing.user_id != user.id:
                raise RechargeError("this payment authority belongs to another user") from None
            session = existing
            redirect = _gateway_payment_link(gateway, authority)
            replayed = True
        else:
            replayed = False

        session_id = session.id
        if session_id is None:  # pragma: no cover - a persisted session always has an id
            raise RechargeError("payment session has no id")
        await emit_safe(
            self._events,
            recharge_created_event(
                user=user,
                payment_session_id=session_id,
                amount_minor=session.amount_minor,
                currency=session.currency,
                gateway=session.gateway_key,
            ),
        )
        logger.info(
            "recharge session %s created for user %s (%d %s, gateway %s, replayed=%s)",
            session.id,
            user.id,
            amount_minor,
            currency,
            resolved_key,
            replayed,
        )
        return RechargeStart(session=session, redirect_url=redirect, replayed=replayed)


__all__ = [
    "RechargeAmountError",
    "RechargeDisabledError",
    "RechargeError",
    "RechargeGateway",
    "RechargeGatewaySelectionRequired",
    "RechargeStart",
    "WalletRechargeService",
]
