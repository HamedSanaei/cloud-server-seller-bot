"""SQLAlchemy repository adapter for payment sessions."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import PaymentSession as _PaymentModel
from cloud_platform.db.timestamps import (
    from_db_utc_or_none,
    to_db_utc,
    to_db_utc_or_none,
    utc_now,
)
from cloud_platform.modules.payments.domain import (
    DuplicateExternalIdError,
    InvalidPaymentSessionTransition,
    PaymentSession,
    PaymentSessionStatus,
)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _to_domain(row: _PaymentModel) -> PaymentSession:
    status = PaymentSessionStatus(_attr(row, "status"))
    # created_at/updated_at/credited_at are legacy TIMESTAMP WITHOUT TIME ZONE
    # (naive UTC) columns; fx_observed_at is genuinely timestamptz. Only the
    # legacy trio is rehydrated as aware UTC.
    credited_at = from_db_utc_or_none(_attr(row, "credited_at"))
    created = from_db_utc_or_none(_attr(row, "created_at"))
    updated = from_db_utc_or_none(_attr(row, "updated_at"))
    # Migration 0036 columns are nullable and absent on legacy rows: read
    # defensively so old sessions stay readable (credit == settlement).
    # isinstance guards matter: test doubles and partial rows may carry
    # non-string sentinels (e.g. MagicMock) for columns they do not model.
    credit_amount = getattr(row, "credit_amount_minor", None)
    credit_currency = getattr(row, "credit_currency", None)
    fx_source_raw = getattr(row, "fx_source", None)
    fx_rate_raw = getattr(row, "fx_rate", None)
    fx_path_raw = getattr(row, "fx_path", None)
    fx_proxy_asset_raw = getattr(row, "fx_proxy_asset", None)
    fx_observed_raw = getattr(row, "fx_observed_at", None)
    details_raw = getattr(row, "payment_details", None)
    redirect = getattr(row, "redirect_url", None)
    tracking = getattr(row, "tracking_code", None)
    session = PaymentSession(
        user_id=_attr(row, "user_id"),
        gateway_key=str(_attr(row, "gateway_key")),
        amount_minor=int(_attr(row, "amount_minor")),
        currency=str(_attr(row, "currency")),
        idempotency_key=str(_attr(row, "idempotency_key")),
        id=_attr(row, "id"),
        gateway_payment_id=_attr(row, "gateway_payment_id"),
        status=status,
        redirect_url=redirect if isinstance(redirect, str) else None,
        tracking_code=tracking if isinstance(tracking, str) else None,
        credit_amount_minor=int(credit_amount)
        if isinstance(credit_amount, int) and not isinstance(credit_amount, bool)
        else None,
        credit_currency=credit_currency if isinstance(credit_currency, str) else None,
        fx_source=fx_source_raw if isinstance(fx_source_raw, str) else None,
        fx_rate=str(fx_rate_raw) if isinstance(fx_rate_raw, (str, int)) else None,
        fx_path=fx_path_raw if isinstance(fx_path_raw, str) else None,
        fx_observed_at=fx_observed_raw if isinstance(fx_observed_raw, datetime) else None,
        fx_proxy=bool(getattr(row, "fx_proxy", False) or False),
        fx_proxy_asset=fx_proxy_asset_raw if isinstance(fx_proxy_asset_raw, str) else None,
        created_at=created,
        updated_at=updated,
        payment_details=dict(details_raw) if isinstance(details_raw, dict) else None,
        provider_payment_id=getattr(row, "provider_payment_id", None)
        if isinstance(getattr(row, "provider_payment_id", None), str)
        else None,
    )
    if credited_at is not None:
        session = session.mark_credited(at=credited_at)
    return session


def _to_row(aggregate: PaymentSession) -> _PaymentModel:
    return _PaymentModel(
        user_id=aggregate.user_id,
        gateway_key=aggregate.gateway_key,
        gateway_payment_id=aggregate.gateway_payment_id,
        redirect_url=aggregate.redirect_url,
        tracking_code=aggregate.tracking_code,
        amount_minor=aggregate.amount_minor,
        currency=aggregate.currency,
        status=aggregate.status.value,
        idempotency_key=aggregate.idempotency_key,
        credited_at=to_db_utc_or_none(aggregate.credited_at),
        credit_amount_minor=aggregate.credit_amount_minor,
        credit_currency=aggregate.credit_currency,
        fx_source=aggregate.fx_source,
        fx_rate=aggregate.fx_rate,
        fx_path=aggregate.fx_path,
        fx_observed_at=aggregate.fx_observed_at,
        fx_proxy=aggregate.fx_proxy,
        fx_proxy_asset=aggregate.fx_proxy_asset,
        payment_details=aggregate.payment_details,
        provider_payment_id=aggregate.provider_payment_id,
    )


def _validate_save(current: PaymentSession, proposed: PaymentSession) -> None:
    """Compare durable financial facts while holding the payment row lock."""
    for field_name in (
        "user_id",
        "gateway_key",
        "currency",
        "idempotency_key",
        "credit_amount_minor",
        "credit_currency",
        "fx_source",
        "fx_rate",
        "fx_path",
        "fx_observed_at",
        "fx_proxy",
        "fx_proxy_asset",
    ):
        if getattr(current, field_name) != getattr(proposed, field_name):
            raise ValueError(f"payment {field_name} is immutable")
    if current.gateway_payment_id is not None:
        for field_name in (
            "gateway_payment_id",
            "amount_minor",
            "payment_details",
            "redirect_url",
            "tracking_code",
        ):
            if getattr(current, field_name) != getattr(proposed, field_name):
                raise ValueError(f"bound invoice {field_name} is immutable")
    elif proposed.amount_minor != current.amount_minor and not proposed.gateway_payment_id:
        raise ValueError("invoice total can change only when binding a provider invoice")
    if current.provider_payment_id is not None and (
        proposed.provider_payment_id != current.provider_payment_id
    ):
        raise ValueError("provider payment identity is immutable")
    if current.status is not PaymentSessionStatus.PENDING and proposed.status is not current.status:
        raise InvalidPaymentSessionTransition("persisted payment state is already terminal")
    if proposed.credited_at is not None and proposed.status is not PaymentSessionStatus.SUCCEEDED:
        raise InvalidPaymentSessionTransition("only succeeded payments can be credited")


class SqlAlchemyPaymentSessionRepository:
    """Durable storage for payment sessions with unique external ids."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def create(self, session: PaymentSession) -> PaymentSession:
        row = _to_row(session)
        async with self._session_factory() as db:
            db.add(row)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise DuplicateExternalIdError(
                    f"payment already exists for "
                    f"({session.gateway_key}, {session.gateway_payment_id})"
                ) from exc
            await db.refresh(row)
            return _to_domain(row)

    async def get(self, session_id: UUID) -> PaymentSession | None:
        async with self._session_factory() as db:
            result = await db.execute(select(_PaymentModel).where(_PaymentModel.id == session_id))
            row = result.scalar_one_or_none()
            return None if row is None else _to_domain(row)

    async def search_admin(
        self,
        query: str,
        *,
        status: str | None = None,
        gateway_key: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[PaymentSession], bool]:
        """Exact invoice/payment/session/owner lookup with bounded stable pagination."""
        if type(offset) is not int or not 0 <= offset <= 10_000:
            raise ValueError("invalid payment offset")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid payment limit")
        if not isinstance(query, str) or len(query) > 100:
            raise ValueError("invalid payment query")
        stmt = select(_PaymentModel)
        if query:
            matches = [
                _PaymentModel.gateway_payment_id == query,
                _PaymentModel.provider_payment_id == query,
            ]
            try:
                identity = UUID(query)
            except ValueError:
                pass
            else:
                matches.extend([_PaymentModel.id == identity, _PaymentModel.user_id == identity])
            stmt = stmt.where(or_(*matches))
        if status is not None:
            stmt = stmt.where(_PaymentModel.status == PaymentSessionStatus(status).value)
        if gateway_key is not None:
            stmt = stmt.where(_PaymentModel.gateway_key == gateway_key)
        stmt = (
            stmt.order_by(_PaymentModel.created_at.desc(), _PaymentModel.id.desc())
            .offset(offset)
            .limit(limit + 1)
        )
        async with self._session_factory() as db:
            rows = list((await db.execute(stmt)).scalars().all())
            return [_to_domain(row) for row in rows[:limit]], len(rows) > limit

    async def get_by_external_id(
        self, gateway_key: str, gateway_payment_id: str
    ) -> PaymentSession | None:
        async with self._session_factory() as db:
            stmt = select(_PaymentModel).where(
                _PaymentModel.gateway_key == gateway_key,
                _PaymentModel.gateway_payment_id == gateway_payment_id,
            )
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            return None if row is None else _to_domain(row)

    async def get_by_idempotency_key(
        self, gateway_key: str, idempotency_key: str
    ) -> PaymentSession | None:
        async with self._session_factory() as db:
            stmt = (
                select(_PaymentModel)
                .where(
                    _PaymentModel.gateway_key == gateway_key,
                    _PaymentModel.idempotency_key == idempotency_key,
                )
                .order_by(_PaymentModel.created_at.desc())
            )
            result = await db.execute(stmt)
            row = result.scalars().first()
            return None if row is None else _to_domain(row)

    async def list_pending_before(
        self, gateway_key: str, before: datetime, limit: int = 100
    ) -> list[PaymentSession]:
        """Pending or succeeded-but-uncredited sessions older than ``before``.

        ``created_at`` is a legacy naive-UTC column, so the aware cutoff is
        normalized at the boundary; binding it verbatim made asyncpg raise
        ``can't subtract offset-naive and offset-aware datetimes`` and killed
        Tetraminator reconciliation.
        """
        cutoff = to_db_utc(before)
        async with self._session_factory() as db:
            stmt = (
                select(_PaymentModel)
                .where(
                    _PaymentModel.gateway_key == gateway_key,
                    or_(
                        _PaymentModel.status == PaymentSessionStatus.PENDING.value,
                        and_(
                            _PaymentModel.status == PaymentSessionStatus.SUCCEEDED.value,
                            _PaymentModel.credited_at.is_(None),
                        ),
                    ),
                    _PaymentModel.created_at <= cutoff,
                    or_(
                        _PaymentModel.gateway_payment_id.is_not(None),
                        _PaymentModel.gateway_key == "nowpayments",
                    ),
                )
                .order_by(_PaymentModel.created_at.asc())
                .limit(limit)
            )
            result = await db.execute(stmt)
            return [_to_domain(row) for row in result.scalars().all()]

    async def save(self, session: PaymentSession) -> PaymentSession:
        assert session.id is not None  # only persisted sessions can be saved
        session_id: UUID = session.id
        async with self._session_factory() as db:
            result = await db.execute(
                select(_PaymentModel).where(_PaymentModel.id == session_id).with_for_update()
            )
            row = result.scalar_one_or_none()
            if row is None:
                raise LookupError(f"payment session {session_id} not found")
            current = _to_domain(row)
            _validate_save(current, session)
            if current.credited_at is not None:
                # Preserve the first credit marker even when an older success
                # races its final save after the winning callback.
                return current
            cast_any: Any = row
            cast_any.status = session.status.value
            cast_any.gateway_payment_id = session.gateway_payment_id
            cast_any.redirect_url = session.redirect_url
            cast_any.tracking_code = session.tracking_code
            cast_any.credited_at = to_db_utc_or_none(session.credited_at)
            cast_any.updated_at = to_db_utc(utc_now())
            # The provider may add a unique suffix to its requested base amount.
            # Binding that invoice total never modifies the frozen wallet credit.
            cast_any.amount_minor = session.amount_minor
            cast_any.payment_details = session.payment_details
            cast_any.provider_payment_id = session.provider_payment_id
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise DuplicateExternalIdError(
                    "provider payment identity is already bound"
                ) from exc
            await db.refresh(row)
            return _to_domain(row)
