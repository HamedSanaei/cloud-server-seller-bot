"""Bounded, redacted payment administration through authoritative inquiry."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from cloud_platform.modules.payments.domain import PaymentSession, PaymentSessionStatus
from cloud_platform.modules.users.domain import PermissionDeniedError, User, UserRepository
from cloud_platform.modules.users.superadmins import require_superadmin, validate_telegram_user_ids

MAX_PAGE_SIZE = 50
MAX_OFFSET = 10_000
MAX_QUERY_LENGTH = 100


@dataclass(frozen=True, slots=True)
class AdminPaymentSummary:
    session_id: UUID
    user_id: UUID
    gateway_key: str
    status: PaymentSessionStatus
    amount_minor: int
    currency: str
    credit_amount_minor: int
    credit_currency: str
    invoice_id: str | None
    created_at: datetime | None
    credited_at: datetime | None


@dataclass(frozen=True, slots=True)
class AdminPaymentPage:
    items: tuple[AdminPaymentSummary, ...]
    offset: int
    limit: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class AdminPaymentInquiry:
    payment: AdminPaymentSummary
    observed_status: str
    settlement_held: bool = False


def payment_summary(session: PaymentSession) -> AdminPaymentSummary:
    if session.id is None:
        raise ValueError("a persisted invoice is required")
    return AdminPaymentSummary(
        session.id,
        session.user_id,
        session.gateway_key,
        session.status,
        session.amount_minor,
        session.currency,
        session.effective_credit_amount,
        session.effective_credit_currency,
        session.gateway_payment_id,
        session.created_at,
        session.credited_at,
    )


class AdminPaymentService:
    """Every operation refreshes configured active persisted administrator RBAC.

    There is deliberately no mark-paid or credit operation. Inquiry delegates
    settlement to the existing verified, idempotent payment path; manual-review
    rows remain held and may only be inspected against their provider.
    """

    def __init__(
        self,
        users: UserRepository,
        payments: Any,
        authoritative: Any,
        *,
        configured_ids: Iterable[int] = (),
    ) -> None:
        self._users, self._payments, self._authoritative = users, payments, authoritative
        self._ids = validate_telegram_user_ids(tuple(configured_ids))

    async def _authorize(self, actor: User) -> User:
        claimed = require_superadmin(actor, self._ids)
        fresh = require_superadmin(await self._users.get(claimed.id), self._ids)
        if fresh.id != claimed.id or fresh.telegram_user_id != claimed.telegram_user_id:
            raise PermissionDeniedError("persisted administrator identity changed")
        return fresh

    async def search(
        self,
        actor: User,
        query: str = "",
        *,
        status: str | PaymentSessionStatus | None = None,
        gateway_key: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> AdminPaymentPage:
        await self._authorize(actor)
        if type(offset) is not int or not 0 <= offset <= MAX_OFFSET:
            raise ValueError("payment offset must be between 0 and 10000")
        if type(limit) is not int or limit < 1:
            raise ValueError("payment limit must be positive")
        if not isinstance(query, str) or len(query.strip()) > MAX_QUERY_LENGTH:
            raise ValueError("invoice query must contain at most 100 characters")
        if gateway_key is not None and (
            not isinstance(gateway_key, str)
            or not 1 <= len(gateway_key) <= 32
            or not gateway_key.replace("_", "").isalnum()
        ):
            raise ValueError("invalid payment gateway filter")
        resolved = None if status is None else PaymentSessionStatus(status)
        bounded_limit = min(limit, MAX_PAGE_SIZE)
        items, has_more = await self._payments.search_admin(
            query.strip(),
            status=resolved.value if resolved else None,
            gateway_key=gateway_key,
            offset=offset,
            limit=bounded_limit,
        )
        return AdminPaymentPage(
            tuple(payment_summary(row) for row in items[:bounded_limit]),
            offset,
            bounded_limit,
            bool(has_more),
        )

    async def profile(self, actor: User, session_id: UUID) -> AdminPaymentSummary:
        await self._authorize(actor)
        return payment_summary(await self._load(session_id))

    async def _load(self, session_id: UUID) -> PaymentSession:
        if not isinstance(session_id, UUID):
            raise ValueError("payment session UUID required")
        session = await self._payments.get(session_id)
        if session is None:
            raise LookupError("payment invoice not found")
        return session

    async def inquire(self, actor: User, session_id: UUID) -> AdminPaymentInquiry:
        await self._authorize(actor)
        session = await self._load(session_id)
        if session.status is PaymentSessionStatus.MANUAL_REVIEW:
            observed = await self._authoritative.inspect_session(session)
            return AdminPaymentInquiry(payment_summary(session), observed.status.value, True)
        saved = await self._authoritative.reconcile_session(session)
        return AdminPaymentInquiry(payment_summary(saved), saved.status.value)
