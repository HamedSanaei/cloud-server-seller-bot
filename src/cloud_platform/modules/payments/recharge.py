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
)

logger = logging.getLogger(__name__)


class RechargeError(Exception):
    """Base error for wallet top-up operations."""


class RechargeDisabledError(RechargeError):
    """No gateway is configured for the wallet's currency."""


class RechargeAmountError(RechargeError):
    """The requested top-up amount is not a positive integer amount."""


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


class WalletRechargeService:
    """Creates pending gateway sessions for wallet top-ups."""

    def __init__(
        self,
        *,
        payments_repo: PaymentSessionRepository,
        gateway: RechargeGateway | None,
        event_sink: BusinessEventSink | None = None,
        user_repo: object | None = None,
    ) -> None:
        self._payments = payments_repo
        self._gateway = gateway
        self._events = event_sink
        self._users = user_repo

    @property
    def gateway_key(self) -> str:
        """Key of the configured gateway (``""`` when none)."""
        return getattr(self._gateway, "key", "") if self._gateway is not None else ""

    def supports_currency(self, currency: str) -> bool:
        """Whether the configured gateway can charge in ``currency``.

        Iranian gateways settle in their own currency, so a wallet in a
        different currency has no online top-up — the UI must say so instead
        of offering a button that cannot work.
        """
        if self._gateway is None:
            return False
        supported = getattr(self._gateway, "supported_currency", "")
        return bool(supported) and str(supported).upper() == str(currency).upper()

    async def start(
        self,
        *,
        user: Any,
        amount_minor: int,
        currency: str,
        idempotency_key: str,
        redirect_url: str | None = None,
    ) -> RechargeStart:
        """Create (or replay) one pending top-up session."""
        if self._gateway is None:
            raise RechargeDisabledError("no payment gateway is configured")
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
        if not self.supports_currency(currency):
            raise RechargeDisabledError(f"gateway {self.gateway_key!r} cannot charge in {currency}")

        intent = await self._gateway.create_payment(
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
            gateway_key=self.gateway_key,
            amount_minor=amount_minor,
            currency=currency,
            idempotency_key=key,
            gateway_payment_id=authority,
        )
        replayed = False
        try:
            session = await self._payments.create(session)
        except DuplicateExternalIdError:
            existing = await self._payments.get_by_external_id(self.gateway_key, authority)
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
            self.gateway_key,
            replayed,
        )
        return RechargeStart(session=session, redirect_url=redirect, replayed=replayed)


__all__ = [
    "RechargeAmountError",
    "RechargeDisabledError",
    "RechargeError",
    "RechargeGateway",
    "RechargeStart",
    "WalletRechargeService",
]
