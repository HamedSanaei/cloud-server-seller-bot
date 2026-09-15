"""Payments domain: persistent sessions tracking gateway-side payment attempts.

A ``PaymentSession`` records one inbound payment attempt against an external
gateway (see ``providers.base.PaymentGateway``). It exists so that gateway
state can be reconciled to wallet deposits safely:

- the external ``(gateway_key, gateway_payment_id)`` pair is UNIQUE at the
  database level, so a replayed webhook or duplicated callback cannot create
  a second session;
- the state machine only permits terminal transitions from ``PENDING``, so
  an already-succeeded session cannot be re-succeeded by a stale callback;
- ``credited_at`` records when the corresponding ledger deposit was posted,
  giving webhook handlers (M09-003) an explicit deduplication marker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class PaymentSessionError(Exception):
    """Base error for payment session operations."""


class InvalidPaymentSessionTransition(PaymentSessionError):
    """Raised when a state transition violates the session lifecycle."""


class DuplicateExternalIdError(PaymentSessionError):
    """Raised when the (gateway_key, gateway_payment_id) pair already exists."""


class PaymentSessionStatus(StrEnum):
    """Lifecycle of an inbound payment attempt."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PaymentSession:
    """One inbound payment attempt, reconciled against the wallet ledger.

    ``amount_minor``/``currency`` are the GATEWAY settlement amount: what the
    provider invoice charges and what inquiry/callback verification compares
    EXACTLY. Cross-currency recharges additionally carry the frozen WALLET
    credit side (``credit_amount_minor``/``credit_currency``) plus the FX
    conversion snapshot: the callback credits the frozen credit side and
    never fetches a new rate. ``None`` credit fields mean a legacy
    same-currency session (credit == settlement).

    Attributes:
        user_id: Wallet owner the deposit belongs to.
        gateway_key: Identifier of the payment gateway (e.g. "zarinpal").
        amount_minor: Gateway settlement amount (positive integer minor units).
        currency: Gateway settlement currency (3-letter uppercase).
        idempotency_key: Key sent to the gateway on creation.
        id: Assigned on persistence.
        gateway_payment_id: External id assigned by the gateway, if known.
        status: Current lifecycle state.
        credited_at: When the matching ledger deposit was posted.
        credit_amount_minor/credit_currency: Frozen wallet credit side.
        fx_source/fx_rate/fx_path/fx_observed_at/fx_proxy/fx_proxy_asset:
            Frozen conversion audit trail (rate as decimal text).
        created_at / updated_at: Persistence timestamps.
    """

    user_id: UUID
    gateway_key: str
    amount_minor: int
    currency: str
    idempotency_key: str
    id: UUID | None = None
    gateway_payment_id: str | None = None
    status: PaymentSessionStatus = PaymentSessionStatus.PENDING
    credited_at: datetime | None = None
    credit_amount_minor: int | None = None
    credit_currency: str | None = None
    fx_source: str | None = None
    fx_rate: str | None = None
    fx_path: str | None = None
    fx_observed_at: datetime | None = None
    fx_proxy: bool = False
    fx_proxy_asset: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.amount_minor <= 0:
            raise ValueError("payment amount must be a positive integer of minor units")
        if len(self.currency) != 3 or not self.currency.isalpha() or not self.currency.isupper():
            raise ValueError("currency must be a 3-letter uppercase ISO-4217 code")
        if not self.gateway_key or not self.gateway_key.strip():
            raise ValueError("gateway_key must not be empty")
        if self.credit_amount_minor is not None and self.credit_amount_minor <= 0:
            raise ValueError("credit amount must be a positive integer of minor units")
        if self.credit_currency is not None and (
            len(self.credit_currency) != 3
            or not self.credit_currency.isalpha()
            or not self.credit_currency.isupper()
        ):
            raise ValueError("credit currency must be a 3-letter uppercase ISO-4217 code")

    @property
    def effective_credit_amount(self) -> int:
        """Wallet credit amount (frozen; legacy sessions read settlement)."""
        if self.credit_amount_minor is not None:
            return self.credit_amount_minor
        return self.amount_minor

    @property
    def effective_credit_currency(self) -> str:
        """Wallet credit currency (frozen; legacy sessions read settlement)."""
        return self.credit_currency if self.credit_currency is not None else self.currency

    def _require_pending(self) -> None:
        if self.status is not PaymentSessionStatus.PENDING:
            raise InvalidPaymentSessionTransition(
                f"payment session {self.id} is {self.status.value}; "
                "only pending sessions can transition"
            )

    def _bind_external_id(self, gateway_payment_id: str) -> None:
        self._require_pending()
        if not gateway_payment_id or not gateway_payment_id.strip():
            raise ValueError("gateway_payment_id must not be empty")
        object.__setattr__(self, "gateway_payment_id", gateway_payment_id)

    def with_gateway_payment_id(self, gateway_payment_id: str) -> PaymentSession:
        """Return a copy bound to the external id, staying PENDING.

        Used when the local intent row is persisted BEFORE the gateway
        call (so the callback URL can reference it) and the gateway id
        arrives afterwards. Requires PENDING, like every other transition.
        """
        self._bind_external_id(gateway_payment_id)
        return PaymentSession(
            user_id=self.user_id,
            gateway_key=self.gateway_key,
            amount_minor=self.amount_minor,
            currency=self.currency,
            idempotency_key=self.idempotency_key,
            id=self.id,
            gateway_payment_id=gateway_payment_id,
            status=self.status,
            credited_at=self.credited_at,
            credit_amount_minor=self.credit_amount_minor,
            credit_currency=self.credit_currency,
            fx_source=self.fx_source,
            fx_rate=self.fx_rate,
            fx_path=self.fx_path,
            fx_observed_at=self.fx_observed_at,
            fx_proxy=self.fx_proxy,
            fx_proxy_asset=self.fx_proxy_asset,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def mark_succeeded(self, *, gateway_payment_id: str) -> PaymentSession:
        """Return a SUCCEEDED copy bound to the external id."""
        self._bind_external_id(gateway_payment_id)
        return PaymentSession(
            user_id=self.user_id,
            gateway_key=self.gateway_key,
            amount_minor=self.amount_minor,
            currency=self.currency,
            idempotency_key=self.idempotency_key,
            id=self.id,
            gateway_payment_id=gateway_payment_id,
            status=PaymentSessionStatus.SUCCEEDED,
            credited_at=self.credited_at,
            credit_amount_minor=self.credit_amount_minor,
            credit_currency=self.credit_currency,
            fx_source=self.fx_source,
            fx_rate=self.fx_rate,
            fx_path=self.fx_path,
            fx_observed_at=self.fx_observed_at,
            fx_proxy=self.fx_proxy,
            fx_proxy_asset=self.fx_proxy_asset,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def mark_failed(self, *, gateway_payment_id: str) -> PaymentSession:
        """Return a FAILED copy bound to the external id."""
        self._bind_external_id(gateway_payment_id)
        return PaymentSession(
            user_id=self.user_id,
            gateway_key=self.gateway_key,
            amount_minor=self.amount_minor,
            currency=self.currency,
            idempotency_key=self.idempotency_key,
            id=self.id,
            gateway_payment_id=gateway_payment_id,
            status=PaymentSessionStatus.FAILED,
            credited_at=self.credited_at,
            credit_amount_minor=self.credit_amount_minor,
            credit_currency=self.credit_currency,
            fx_source=self.fx_source,
            fx_rate=self.fx_rate,
            fx_path=self.fx_path,
            fx_observed_at=self.fx_observed_at,
            fx_proxy=self.fx_proxy,
            fx_proxy_asset=self.fx_proxy_asset,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    def mark_credited(self, *, at: datetime) -> PaymentSession:
        """Record that the ledger deposit was posted for this session."""
        if self.status is not PaymentSessionStatus.SUCCEEDED:
            raise InvalidPaymentSessionTransition(
                f"payment session {self.id} is {self.status.value}; "
                "only succeeded sessions can be marked credited"
            )
        return PaymentSession(
            user_id=self.user_id,
            gateway_key=self.gateway_key,
            amount_minor=self.amount_minor,
            currency=self.currency,
            idempotency_key=self.idempotency_key,
            id=self.id,
            gateway_payment_id=self.gateway_payment_id,
            status=self.status,
            credited_at=at,
            credit_amount_minor=self.credit_amount_minor,
            credit_currency=self.credit_currency,
            fx_source=self.fx_source,
            fx_rate=self.fx_rate,
            fx_path=self.fx_path,
            fx_observed_at=self.fx_observed_at,
            fx_proxy=self.fx_proxy,
            fx_proxy_asset=self.fx_proxy_asset,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class PaymentSessionRepository(Protocol):
    """Port for durable payment sessions."""

    async def create(self, session: PaymentSession) -> PaymentSession:
        """Persist a new session. Raises DuplicateExternalIdError on clash."""
        ...

    async def get(self, session_id: UUID) -> PaymentSession | None:
        """Fetch by primary key."""
        ...

    async def get_by_external_id(
        self, gateway_key: str, gateway_payment_id: str
    ) -> PaymentSession | None:
        """Fetch by unique external identity pair."""
        ...

    async def get_by_idempotency_key(
        self, gateway_key: str, idempotency_key: str
    ) -> PaymentSession | None:
        """Fetch the latest session for a local idempotency key, if any."""
        ...

    async def list_pending_before(
        self, gateway_key: str, before: datetime, limit: int = 100
    ) -> list[PaymentSession]:
        """Oldest PENDING sessions of one gateway (bounded reconciliation scan)."""
        ...

    async def save(self, session: PaymentSession) -> PaymentSession:
        """Persist the current state of a session aggregate."""
        ...


def session_credit_amount(session: object) -> int:
    """Frozen wallet credit amount for any session shape.

    Prefers the persisted credit side; falls back to the settlement amount
    for legacy rows and test doubles that predate the FX snapshot columns.
    """
    getter = getattr(session, "effective_credit_amount", None)
    if isinstance(getter, int) and not isinstance(getter, bool) and getter > 0:
        return getter
    credit = getattr(session, "credit_amount_minor", None)
    if isinstance(credit, int) and not isinstance(credit, bool) and credit > 0:
        return credit
    amount = getattr(session, "amount_minor", 0)
    return int(amount) if isinstance(amount, int) and not isinstance(amount, bool) else 0


def session_credit_currency(session: object) -> str:
    """Frozen wallet credit currency for any session shape."""
    eff = getattr(session, "effective_credit_currency", None)
    if isinstance(eff, str) and eff.strip():
        return eff.strip().upper()
    credit = getattr(session, "credit_currency", None)
    if isinstance(credit, str) and credit.strip():
        return credit.strip().upper()
    currency = getattr(session, "currency", "")
    return str(currency).strip().upper() if isinstance(currency, str) else ""
