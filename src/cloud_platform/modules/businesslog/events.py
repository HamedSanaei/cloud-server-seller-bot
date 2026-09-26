"""Business-event builders (one place per event shape).

Every builder produces the SAME payload shape for its event type, keeps
money as integer minor units rendered through :func:`format_minor` (never a
float), and only accepts identifiers already known to the caller. Credentials,
root passwords and provider secrets are never parameters of any builder — a
call site physically cannot pass one in.

The deterministic ``event_key`` is what makes the outbox idempotent, so it is
derived from the durable local identity (server id, payment session id,
operation key) rather than from anything time-based.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cloud_platform.modules.businesslog.domain import (
    BusinessEvent,
    BusinessEventType,
    compact,
    format_minor,
)

__all__ = [
    "admin_adjustment_event",
    "provider_accepted_event",
    "purchase_failed_event",
    "purchase_requested_event",
    "recharge_created_event",
    "recharge_failed_event",
    "recharge_succeeded_event",
    "server_management_event",
    "server_operation_failed_event",
    "service_renewal_event",
    "vps_provisioned_event",
]


def _at(moment: datetime | None) -> str:
    return (moment or datetime.now(UTC)).isoformat()


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _user_fields(user: Any, user_id: UUID | str | None = None) -> dict[str, Any]:
    """Identity fields of a platform user (never a credential).

    Worker/reconciler call sites may hold only the durable ``user_id`` of a
    server; ``user_id`` is then the fallback identity (the richer Telegram
    fields are added only when a loaded user is available).
    """
    fields: dict[str, Any] = {}
    if user is not None:
        fields = compact(
            user_id=_text(getattr(user, "id", None)),
            telegram_user_id=getattr(user, "telegram_user_id", None),
            username=getattr(user, "username", None),
        )
    if user_id is not None and "user_id" not in fields:
        fields["user_id"] = _text(user_id)
    return fields


def purchase_requested_event(
    *,
    user: Any,
    server_id: UUID | str,
    order_id: UUID | str | None = None,
    provider_key: str,
    market: str = "",
    location_id: str | None = None,
    plan_name: str | None = None,
    product_id: str | None = None,
    os_name: str | None = None,
    image_id: str | None = None,
    selling_price_minor: int | None = None,
    currency: str | None = None,
    provider_cost_minor: int | None = None,
    provider_cost_currency: str | None = None,
    credential_account: str | None = None,
    operation_key: str | None = None,
    kind: str | None = None,
    user_id: UUID | str | None = None,
    at: datetime | None = None,
) -> BusinessEvent:
    """The customer confirmed a purchase and a durable intent now exists."""
    return BusinessEvent(
        event_key=f"purchase.requested:{server_id}",
        event_type=BusinessEventType.PURCHASE_REQUESTED,
        payload=compact(
            at=_at(at),
            kind=kind,
            market=market,
            provider=provider_key,
            credential_account=credential_account,
            location=location_id,
            plan=plan_name,
            product_id=product_id,
            os=os_name,
            image=image_id,
            selling_price=(
                format_minor(selling_price_minor, currency) if selling_price_minor else None
            ),
            currency=currency,
            provider_cost=(
                format_minor(provider_cost_minor, provider_cost_currency)
                if provider_cost_minor is not None
                else None
            ),
            server_id=_text(server_id),
            order_id=_text(order_id),
            operation_key=operation_key,
            **_user_fields(user, user_id),
        ),
        created_at=at,
    )


def provider_accepted_event(
    *,
    user: Any,
    server_id: UUID | str,
    order_id: UUID | str | None = None,
    provider_key: str,
    provider_order_id: str | None = None,
    product_id: str | None = None,
    location_id: str | None = None,
    plan_name: str | None = None,
    provider_cost_minor: int | None = None,
    currency: str | None = None,
    operation_key: str | None = None,
    credential_account: str | None = None,
    image_id: str | None = None,
    state: str | None = None,
    kind: str | None = None,
    user_id: UUID | str | None = None,
    at: datetime | None = None,
) -> BusinessEvent:
    """The provider accepted the order and the order id is durably stored."""
    return BusinessEvent(
        event_key=f"purchase.provider_accepted:{server_id}",
        event_type=BusinessEventType.PROVIDER_ACCEPTED,
        payload=compact(
            at=_at(at),
            kind=kind,
            provider=provider_key,
            credential_account=credential_account,
            provider_order_id=provider_order_id,
            product_id=product_id,
            location=location_id,
            plan=plan_name,
            image=image_id,
            state=state,
            provider_cost=(
                format_minor(provider_cost_minor, currency)
                if provider_cost_minor is not None
                else None
            ),
            currency=currency,
            server_id=_text(server_id),
            order_id=_text(order_id),
            operation_key=operation_key,
            **_user_fields(user, user_id),
        ),
        created_at=at,
    )


def vps_provisioned_event(
    *,
    user: Any,
    server_id: UUID | str,
    provider_key: str,
    provider_order_id: str | None,
    location_id: str | None = None,
    plan_name: str | None = None,
    state: str,
    ipv4: str | None = None,
    ipv6: str | None = None,
    credential_account: str | None = None,
    image_id: str | None = None,
    kind: str | None = None,
    user_id: UUID | str | None = None,
    at: datetime | None = None,
) -> BusinessEvent:
    """The VPS is provisioned and activated (delivery reached the customer)."""
    return BusinessEvent(
        event_key=f"purchase.vps_provisioned:{server_id}",
        event_type=BusinessEventType.VPS_PROVISIONED,
        payload=compact(
            at=_at(at),
            kind=kind,
            provider=provider_key,
            credential_account=credential_account,
            provider_order_id=provider_order_id,
            server_id=_text(server_id),
            location=location_id,
            plan=plan_name,
            image=image_id,
            state=state,
            ipv4=ipv4,
            ipv6=ipv6,
            **_user_fields(user, user_id),
        ),
        created_at=at,
    )


def purchase_failed_event(
    *,
    user: Any,
    server_id: UUID | str,
    order_id: UUID | str | None = None,
    provider_key: str,
    provider_order_id: str | None = None,
    operation_key: str | None = None,
    category: str,
    reason: str,
    market: str = "",
    stage: str | None = None,
    location_id: str | None = None,
    plan_name: str | None = None,
    product_id: str | None = None,
    image_id: str | None = None,
    error_code: str | None = None,
    correlation_id: str | None = None,
    credential_account: str | None = None,
    kind: str | None = None,
    user_id: UUID | str | None = None,
    at: datetime | None = None,
) -> BusinessEvent:
    """Definitive failure or an ambiguous outcome requiring a human.

    ``category`` is a short, safe label (never a raw HTTP body) so the
    operator can triage: ``provider_capacity``, ``provider_rejected``,
    ``provider_auth``, ``offer_revalidation_failed``, ``image_unavailable``,
    ``invalid_contract``, ``infrastructure_failure``, ``outcome_unknown``,
    ``recovery_required``. The event key is category-scoped, so the same
    server can report a definitive failure and (later) an ambiguity, while
    every repeated reconciliation pass of the SAME failure dedupes.
    """
    return BusinessEvent(
        event_key=f"purchase.failed:{server_id}:{category}",
        event_type=BusinessEventType.PURCHASE_FAILED,
        payload=compact(
            at=_at(at),
            kind=kind,
            market=market,
            provider=provider_key,
            credential_account=credential_account,
            location=location_id,
            plan=plan_name,
            product_id=product_id,
            image=image_id,
            stage=stage,
            category=category,
            reason=reason,
            error_code=error_code,
            correlation_id=correlation_id,
            server_id=_text(server_id),
            order_id=_text(order_id),
            provider_order_id=provider_order_id,
            operation_key=operation_key,
            **_user_fields(user, user_id),
        ),
        created_at=at,
    )


def recharge_created_event(
    *,
    user: Any,
    payment_session_id: UUID | str,
    amount_minor: int,
    currency: str,
    gateway: str,
    at: datetime | None = None,
) -> BusinessEvent:
    """A wallet recharge session was created at the gateway."""
    return BusinessEvent(
        event_key=f"recharge.created:{payment_session_id}",
        event_type=BusinessEventType.RECHARGE_CREATED,
        payload=compact(
            at=_at(at),
            amount=format_minor(amount_minor, currency),
            currency=currency,
            gateway=gateway,
            payment_session_id=_text(payment_session_id),
            **_user_fields(user),
        ),
        created_at=at,
    )


def recharge_succeeded_event(
    *,
    user: Any,
    payment_session_id: UUID | str,
    amount_minor: int,
    currency: str,
    gateway: str,
    gateway_reference: str | None = None,
    balance_after_minor: int | None = None,
    at: datetime | None = None,
) -> BusinessEvent:
    """The wallet was ACTUALLY credited (never emitted before the credit)."""
    return BusinessEvent(
        event_key=f"recharge.succeeded:{payment_session_id}",
        event_type=BusinessEventType.RECHARGE_SUCCEEDED,
        payload=compact(
            at=_at(at),
            amount=format_minor(amount_minor, currency),
            currency=currency,
            gateway=gateway,
            payment_session_id=_text(payment_session_id),
            gateway_reference=gateway_reference,
            balance_after=(
                format_minor(balance_after_minor, currency)
                if balance_after_minor is not None
                else None
            ),
            **_user_fields(user),
        ),
        created_at=at,
    )


def recharge_failed_event(
    *,
    user: Any,
    payment_session_id: UUID | str,
    amount_minor: int,
    currency: str,
    gateway: str,
    state: str,
    at: datetime | None = None,
) -> BusinessEvent:
    """A recharge attempt failed at the gateway."""
    return BusinessEvent(
        event_key=f"recharge.failed:{payment_session_id}:{state}",
        event_type=BusinessEventType.RECHARGE_FAILED,
        payload=compact(
            at=_at(at),
            amount=format_minor(amount_minor, currency),
            currency=currency,
            gateway=gateway,
            payment_session_id=_text(payment_session_id),
            state=state,
            **_user_fields(user),
        ),
        created_at=at,
    )


def server_management_event(
    *,
    event_type: BusinessEventType,
    event_key_parts: tuple[object, ...],
    user: Any,
    server_id: UUID | str,
    provider_key: str,
    state: str,
    operation: str,
    result: str,
    snapshot: str | None = None,
    image: str | None = None,
    ip: str | None = None,
    at: datetime | None = None,
) -> BusinessEvent:
    """A customer server-management mutation (never a read-only page view).

    The event key is deterministic in the durable local identity plus the
    operation and its outcome, so a double tap, a retried callback or a
    replayed confirmation can never post the same card twice. No secret is a
    parameter of this builder: a console URL, a password or an API key
    physically cannot be logged.
    """
    return BusinessEvent(
        event_key="server-management:" + ":".join(str(part) for part in event_key_parts),
        event_type=event_type,
        payload=compact(
            at=_at(at),
            provider=provider_key,
            server_id=_text(server_id),
            state=state,
            operation=operation,
            result=result,
            snapshot=snapshot,
            image=image,
            ip=ip,
            **_user_fields(user),
        ),
        created_at=at,
    )


def service_renewal_event(
    *,
    event_type: BusinessEventType,
    event_key_parts: tuple[object, ...],
    user: Any,
    server_id: UUID | str,
    provider_key: str,
    state: str,
    result: str,
    amount_minor: int | None = None,
    currency: str | None = None,
    period_end: datetime | None = None,
    grace_until: datetime | None = None,
    days_left: int | None = None,
    auto_renew: bool | None = None,
    at: datetime | None = None,
) -> BusinessEvent:
    """One COMMERCIAL service-lifecycle notice (§39).

    The amount is the LOCAL sale price of the period rendered through
    :func:`format_minor` (integer minor units, never a float and never a
    re-quoted provider price). The builder has no parameter for a wallet
    secret, credential, console URL or provider key, so those physically
    cannot be logged. The event key is deterministic in the durable local
    identity plus the period and outcome, so a repeated worker pass or a
    double tap posts the card once.
    """
    return BusinessEvent(
        event_key="service-renewal:" + ":".join(str(part) for part in event_key_parts),
        event_type=event_type,
        payload=compact(
            at=_at(at),
            provider=provider_key,
            server_id=_text(server_id),
            state=state,
            result=result,
            amount=(
                format_minor(amount_minor, currency)
                if amount_minor is not None and currency
                else None
            ),
            currency=currency,
            period_end=period_end.isoformat() if period_end else None,
            grace_until=grace_until.isoformat() if grace_until else None,
            days_left=days_left,
            auto_renew=None if auto_renew is None else ("on" if auto_renew else "off"),
            **_user_fields(user),
        ),
        created_at=at,
    )


def server_operation_failed_event(
    *,
    user: Any,
    server_id: UUID | str,
    provider_key: str,
    state: str,
    operation: str,
    category: str,
    reason: str,
    at: datetime | None = None,
) -> BusinessEvent:
    """A server-management failure or an outcome that needs a human.

    ``category`` is a short safe label (``provider_rejected``,
    ``outcome_unknown``) — never a raw provider body.
    """
    return BusinessEvent(
        event_key=f"server-management-failed:{server_id}:{operation}:{category}",
        event_type=BusinessEventType.SERVER_OPERATION_FAILED,
        payload=compact(
            at=_at(at),
            provider=provider_key,
            server_id=_text(server_id),
            state=state,
            operation=operation,
            category=category,
            reason=reason,
            **_user_fields(user),
        ),
        created_at=at,
    )


def admin_adjustment_event(
    *,
    admin: Any,
    user: Any,
    amount_minor: int,
    currency: str,
    entry_type: str,
    reason: str,
    balance_after_minor: int | None = None,
    idempotency_key: str,
    at: datetime | None = None,
) -> BusinessEvent:
    """An admin credited or debited a wallet (audited, ledger-backed)."""
    return BusinessEvent(
        event_key=f"admin.wallet_adjustment:{idempotency_key}",
        event_type=BusinessEventType.ADMIN_WALLET_ADJUSTMENT,
        payload=compact(
            at=_at(at),
            admin_id=_text(getattr(admin, "id", None)),
            actor=getattr(admin, "username", None),
            amount=format_minor(amount_minor, currency),
            currency=currency,
            entry_type=entry_type,
            reason=reason,
            balance_after=(
                format_minor(balance_after_minor, currency)
                if balance_after_minor is not None
                else None
            ),
            **_user_fields(user),
        ),
        created_at=at,
    )
