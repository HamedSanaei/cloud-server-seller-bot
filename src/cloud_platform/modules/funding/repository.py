"""SQLAlchemy adapter for operator funding (grants and immutable expenses).

Organisation-funded provider costs are recorded as idempotent authorization
grants and immutable expenses. Nothing here credits a customer wallet, creates
a hold, posts a ledger entry or invents cash: the records are organisation-cost
evidence only.

Concurrency and audit rules implemented here:

* reservation locks and authorizes shared Super Admin policy first, then takes
  the funding advisory lock and grant row locks in that order;
* settlement/cancellation take funding then grant locks and never policy locks;
* reservation, settlement, cancellation and their audit rows commit together,
  so a failing audit insertion rolls the whole mutation back;
* settlement does not re-require current admin privileges (a revocation must
  not strand a real, already-accepted provider cost); a later dispatcher is
  responsible for re-checking NEW provider spending before dispatch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import AuditEvent as _AuditModel
from cloud_platform.db.base import OperatorExpense as _ExpenseModel
from cloud_platform.db.base import OperatorFundingCutover as _CutoverModel
from cloud_platform.db.base import OperatorFundingGrant as _GrantModel
from cloud_platform.db.timestamps import from_db_utc, to_db_utc, utc_now
from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.funding.domain import (
    MAX_KEY_LENGTH,
    MAX_REASON_LENGTH,
    FundingAuthorizationError,
    FundingFactMismatchError,
    FundingGrantNotFoundError,
    FundingGrantState,
    FundingIdempotencyConflictError,
    FundingPurpose,
    FundingSource,
    FundingStateError,
    FundingValidationError,
    OperatorExpense,
    OperatorFundingCutover,
    OperatorFundingGrant,
    ProviderCostSnapshot,
    _require_bounded_text,
    _require_currency,
    _require_positive_int64,
    _require_uuid,
    expense_operation_key,
)
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.modules.users.superadmin_repository import authorize_superadmin_transaction

#: PostgreSQL accepts a *signed* BIGINT. A stable namespace serializes all
#: operator-funding mutations across processes before any funding row lock.
FUNDING_ADVISORY_LOCK_KEY = int.from_bytes(
    sha256(b"cloud-platform:operator-funding-policy:v1").digest()[:8],
    byteorder="big",
    signed=True,
)

#: Upper bound for one reporting page of expenses.
MAX_EXPENSE_PAGE = 1000


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _expense_operation_key(grant_operation_key: str) -> str:
    return expense_operation_key(grant_operation_key)


def _grant_to_domain(row: Any) -> OperatorFundingGrant:
    return OperatorFundingGrant(
        id=cast(UUID, _attr(row, "id")),
        operator_user_id=cast(UUID, _attr(row, "operator_user_id")),
        owner_user_id=cast(UUID, _attr(row, "owner_user_id")),
        operation_key=str(_attr(row, "operation_key")),
        resource_key=str(_attr(row, "resource_key")),
        purpose=FundingPurpose(str(_attr(row, "purpose"))),
        nominal_amount_minor=int(_attr(row, "nominal_amount_minor")),
        nominal_currency=str(_attr(row, "nominal_currency")),
        provider_cost_snapshot=ProviderCostSnapshot.from_dict(
            cast(Mapping[str, Any], _attr(row, "provider_cost_snapshot"))
        ),
        created_at=from_db_utc(_attr(row, "created_at")),
        state=FundingGrantState(str(_attr(row, "state"))),
    )


def _expense_to_domain(row: Any) -> OperatorExpense:
    return OperatorExpense(
        id=cast(UUID, _attr(row, "id")),
        grant_id=cast(UUID, _attr(row, "grant_id")),
        operation_key=str(_attr(row, "operation_key")),
        owner_user_id=cast(UUID, _attr(row, "owner_user_id")),
        operator_user_id=cast(UUID, _attr(row, "operator_user_id")),
        resource_key=str(_attr(row, "resource_key")),
        purpose=FundingPurpose(str(_attr(row, "purpose"))),
        nominal_amount_minor=int(_attr(row, "nominal_amount_minor")),
        nominal_currency=str(_attr(row, "nominal_currency")),
        provider_cost_snapshot=ProviderCostSnapshot.from_dict(
            cast(Mapping[str, Any], _attr(row, "provider_cost_snapshot"))
        ),
        occurred_at=from_db_utc(_attr(row, "occurred_at")),
    )


async def _lock_funding_policy(session: AsyncSession) -> None:
    """Acquire the shared funding policy lock before any funding row lock."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": FUNDING_ADVISORY_LOCK_KEY},
    )


def _snapshot_of(snapshot: ProviderCostSnapshot | Mapping[str, Any]) -> ProviderCostSnapshot:
    if isinstance(snapshot, ProviderCostSnapshot):
        return snapshot
    return ProviderCostSnapshot.from_dict(snapshot)


def _check_grant_facts(
    row: Any,
    *,
    owner_id: UUID,
    purpose: FundingPurpose,
    resource_key: str,
    amount: int,
    currency: str,
    snapshot: ProviderCostSnapshot,
    key: str,
) -> None:
    """Fail closed when a consumed key carries facts other than the request."""
    persisted = ProviderCostSnapshot.from_dict(
        cast(Mapping[str, Any], _attr(row, "provider_cost_snapshot"))
    )
    if (
        _attr(row, "operator_user_id") != owner_id
        or _attr(row, "owner_user_id") != owner_id
        or str(_attr(row, "purpose")) != purpose.value
        or str(_attr(row, "resource_key")) != resource_key
        or int(_attr(row, "nominal_amount_minor")) != amount
        or str(_attr(row, "nominal_currency")) != currency
        or persisted != snapshot
    ):
        raise FundingIdempotencyConflictError(
            f"operation key {key!r} already exists with different facts"
        )


def _check_grant_matches_domain(row: Any, grant: OperatorFundingGrant) -> None:
    """Every immutable fact of a persisted grant must equal the caller's copy."""
    _check_grant_facts(
        row,
        owner_id=grant.owner_user_id,
        purpose=grant.purpose,
        resource_key=grant.resource_key,
        amount=grant.nominal_amount_minor,
        currency=grant.nominal_currency,
        snapshot=grant.provider_cost_snapshot,
        key=grant.operation_key,
    )
    if _attr(row, "id") != grant.id or from_db_utc(_attr(row, "created_at")) != grant.created_at:
        raise FundingFactMismatchError("persisted grant identity differs from the expected grant")


def _checked_expense(row: Any, grant: OperatorFundingGrant) -> OperatorExpense:
    """A replay is successful only if every immutable expense fact agrees."""
    expense = _expense_to_domain(row)
    if (
        expense.grant_id != grant.id
        or expense.operation_key != expense_operation_key(grant.operation_key)
        or expense.operator_user_id != grant.operator_user_id
        or expense.owner_user_id != grant.owner_user_id
        or expense.resource_key != grant.resource_key
        or expense.purpose != grant.purpose
        or expense.nominal_amount_minor != grant.nominal_amount_minor
        or expense.nominal_currency != grant.nominal_currency
        or expense.provider_cost_snapshot != grant.provider_cost_snapshot
    ):
        raise FundingFactMismatchError("expense evidence differs from its immutable grant")
    return expense


def _audit(
    session: AsyncSession,
    *,
    actor_type: ActorType,
    actor_id: UUID,
    action: str,
    resource_type: str,
    resource_id: str,
    reason: str,
    metadata: dict[str, Any],
) -> None:
    session.add(
        _AuditModel(
            id=uuid4(),
            actor_type=actor_type.value,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            reason=reason,
            event_metadata=metadata,
            occurred_at=to_db_utc(utc_now()),
        )
    )


class SqlAlchemyOperatorFundingRepository:
    """Durable, audited operator-funding grants and immutable expenses."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
        *,
        configured_ids: Iterable[int],
    ) -> None:
        self._session_factory = session_factory
        self._configured_ids = tuple(configured_ids)

    # ------------------------------------------------------------------
    # Eligibility
    # ------------------------------------------------------------------

    async def eligible(self, owner_id: UUID) -> bool:
        """Whether the owner passes fresh configured active RBAC.

        Only ``PermissionDeniedError`` means "not eligible". Any other error is
        an infrastructure failure and propagates: eligibility must never
        fail open.
        """
        _require_uuid(owner_id, "owner_id")
        async with self._session_factory() as session, session.begin():
            try:
                await authorize_superadmin_transaction(session, owner_id, self._configured_ids)
            except PermissionDeniedError:
                return False
        return True

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_grant(self, operation_key: str) -> OperatorFundingGrant | None:
        key = _require_bounded_text(operation_key, "operation_key", max_length=MAX_KEY_LENGTH)
        async with self._session_factory() as session:
            row = (
                await session.execute(select(_GrantModel).where(_GrantModel.operation_key == key))
            ).scalar_one_or_none()
            return None if row is None else _grant_to_domain(row)

    async def get_cutover(self, server_id: UUID) -> OperatorFundingCutover | None:
        _require_uuid(server_id, "server_id")
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(_CutoverModel)
                    .where(_CutoverModel.server_id == server_id)
                    .order_by(_CutoverModel.effective_at.desc(), _CutoverModel.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return OperatorFundingCutover(
                id=_attr(row, "id"),
                operation_key=_attr(row, "operation_key"),
                server_id=_attr(row, "server_id"),
                operator_user_id=_attr(row, "operator_user_id"),
                prior_funding_source=FundingSource(_attr(row, "prior_funding_source")),
                effective_at=from_db_utc(_attr(row, "effective_at")),
                reason=_attr(row, "reason"),
                metadata=_attr(row, "cutover_metadata"),
            )

    async def get_expense(self, operation_key: str) -> OperatorExpense | None:
        key = _require_bounded_text(operation_key, "operation_key", max_length=MAX_KEY_LENGTH)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(_ExpenseModel).where(_ExpenseModel.operation_key == key)
                )
            ).scalar_one_or_none()
            return None if row is None else _expense_to_domain(row)

    # ------------------------------------------------------------------
    # Reservation
    # ------------------------------------------------------------------

    async def reserve(
        self,
        *,
        operator_id: UUID,
        owner_id: UUID,
        operation_key: str,
        resource_key: str,
        purpose: FundingPurpose,
        nominal_amount_minor: int,
        nominal_currency: str,
        provider_cost_snapshot: ProviderCostSnapshot | Mapping[str, Any],
        reason: str | None = None,
    ) -> OperatorFundingGrant:
        """Authorize operator-funded work, idempotent on ``operation_key``."""
        _require_uuid(operator_id, "operator_id")
        _require_uuid(owner_id, "owner_id")
        if operator_id != owner_id:
            raise FundingAuthorizationError("operator funding grants are self-only")
        key = _require_bounded_text(operation_key, "operation_key", max_length=MAX_KEY_LENGTH)
        resource = _require_bounded_text(resource_key, "resource_key", max_length=MAX_KEY_LENGTH)
        try:
            resolved_purpose = (
                purpose if isinstance(purpose, FundingPurpose) else FundingPurpose(purpose)
            )
        except ValueError as exc:
            raise FundingValidationError("unknown funding purpose") from exc
        amount = _require_positive_int64(nominal_amount_minor, "nominal_amount_minor")
        currency = _require_currency(nominal_currency, "nominal_currency")
        snapshot = _snapshot_of(provider_cost_snapshot)
        reason_text = _require_bounded_text(
            snapshot.provenance if reason is None else reason,
            "reason",
            max_length=MAX_REASON_LENGTH,
        )

        async with self._session_factory() as session:
            try:
                await authorize_superadmin_transaction(session, operator_id, self._configured_ids)
            except PermissionDeniedError as exc:
                raise FundingAuthorizationError(
                    "configured active self-owned operator required"
                ) from exc
            await _lock_funding_policy(session)
            existing = (
                await session.execute(
                    select(_GrantModel).where(_GrantModel.operation_key == key).with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                _check_grant_facts(
                    existing,
                    owner_id=owner_id,
                    purpose=resolved_purpose,
                    resource_key=resource,
                    amount=amount,
                    currency=currency,
                    snapshot=snapshot,
                    key=key,
                )
                if FundingGrantState(str(_attr(existing, "state"))) is FundingGrantState.CANCELLED:
                    await session.rollback()
                    raise FundingStateError(
                        "a cancelled operator funding grant cannot be re-reserved"
                    )
                existing_grant = _grant_to_domain(existing)
                if existing_grant.state is FundingGrantState.SETTLED:
                    expense = await self._existing_expense(session, existing_grant.id)
                    if expense is None:
                        raise FundingFactMismatchError("settled grant has no expense evidence")
                    _checked_expense(expense, existing_grant)
                return existing_grant

            grant_id = uuid4()
            row = _GrantModel(
                id=grant_id,
                operation_key=key,
                operator_user_id=operator_id,
                owner_user_id=owner_id,
                resource_key=resource,
                purpose=resolved_purpose.value,
                nominal_amount_minor=amount,
                nominal_currency=currency,
                provider_cost_snapshot=snapshot.to_dict(),
                state=FundingGrantState.AUTHORIZED.value,
                created_at=to_db_utc(utc_now()),
            )
            session.add(row)
            _audit(
                session,
                actor_type=ActorType.ADMIN,
                actor_id=operator_id,
                action="operator_funding.grant.reserve",
                resource_type="operator_funding_grant",
                resource_id=str(grant_id),
                reason=reason_text,
                metadata={
                    "operation_key": key,
                    "owner_user_id": str(owner_id),
                    "resource_key": resource,
                    "purpose": resolved_purpose.value,
                    "nominal_amount_minor": str(amount),
                    "nominal_currency": currency,
                },
            )
            # Advisory locks serialize supported writers. An IntegrityError is
            # a real invariant/audit failure, never evidence of a valid replay.
            await session.commit()
            await session.refresh(row)
            return _grant_to_domain(row)

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def settle(self, grant: OperatorFundingGrant) -> OperatorExpense:
        """Settle an authorized grant into exactly one immutable expense.

        Settlement does NOT require current admin privileges: an in-flight,
        already-accepted workload must be able to record its real provider cost
        even after the operator's role was revoked. A later dispatcher checks
        NEW spending before dispatch.
        """
        key = grant.operation_key
        async with self._session_factory() as session:
            await _lock_funding_policy(session)
            row = (
                await session.execute(
                    select(_GrantModel).where(_GrantModel.operation_key == key).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise FundingGrantNotFoundError(f"grant {key!r} not found")
            _check_grant_matches_domain(row, grant)
            state = FundingGrantState(str(_attr(row, "state")))
            if state is FundingGrantState.CANCELLED:
                await session.rollback()
                raise FundingStateError("a cancelled grant cannot be settled")
            if state is FundingGrantState.SETTLED:
                existing = await self._existing_expense(session, _attr(row, "id"))
                if existing is None:
                    raise FundingFactMismatchError("settled grant has no expense evidence")
                return _checked_expense(existing, grant)

            expense_id = uuid4()
            session.add(
                _ExpenseModel(
                    id=expense_id,
                    grant_id=_attr(row, "id"),
                    operation_key=_expense_operation_key(key),
                    owner_user_id=_attr(row, "owner_user_id"),
                    operator_user_id=_attr(row, "operator_user_id"),
                    resource_key=_attr(row, "resource_key"),
                    purpose=_attr(row, "purpose"),
                    nominal_amount_minor=_attr(row, "nominal_amount_minor"),
                    nominal_currency=_attr(row, "nominal_currency"),
                    provider_cost_snapshot=grant.provider_cost_snapshot.to_dict(),
                    occurred_at=to_db_utc(utc_now()),
                )
            )
            cast(Any, row).state = FundingGrantState.SETTLED.value
            _audit(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=_attr(row, "operator_user_id"),
                action="operator_funding.grant.settle",
                resource_type="operator_expense",
                resource_id=str(expense_id),
                reason="Record immutable organisation cost for operator-funded work",
                metadata={
                    "operation_key": key,
                    "grant_id": str(_attr(row, "id")),
                    "resource_key": str(_attr(row, "resource_key")),
                    "purpose": str(_attr(row, "purpose")),
                    "nominal_amount_minor": str(_attr(row, "nominal_amount_minor")),
                    "nominal_currency": str(_attr(row, "nominal_currency")),
                },
            )
            await session.commit()
            await session.refresh(row)
            persisted = await self._existing_expense(session, _attr(row, "id"))
            if persisted is None:
                raise FundingFactMismatchError("settlement produced no expense evidence")
            return _checked_expense(persisted, grant)

    @staticmethod
    async def _existing_expense(session: AsyncSession, grant_id: Any) -> Any:
        return (
            await session.execute(select(_ExpenseModel).where(_ExpenseModel.grant_id == grant_id))
        ).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    async def cancel(self, grant: OperatorFundingGrant, *, reason: str) -> OperatorFundingGrant:
        """Cancel an un-settled grant; never erases a settled expense."""
        reason_text = _require_bounded_text(reason, "reason", max_length=MAX_REASON_LENGTH)
        key = grant.operation_key
        async with self._session_factory() as session:
            await _lock_funding_policy(session)
            row = (
                await session.execute(
                    select(_GrantModel).where(_GrantModel.operation_key == key).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise FundingGrantNotFoundError(f"grant {key!r} not found")
            _check_grant_matches_domain(row, grant)
            state = FundingGrantState(str(_attr(row, "state")))
            if state is FundingGrantState.SETTLED:
                await session.rollback()
                raise FundingStateError("a settled grant cannot be cancelled")
            if state is FundingGrantState.CANCELLED:
                return _grant_to_domain(row)

            cast(Any, row).state = FundingGrantState.CANCELLED.value
            _audit(
                session,
                actor_type=ActorType.SYSTEM,
                actor_id=_attr(row, "operator_user_id"),
                action="operator_funding.grant.cancel",
                resource_type="operator_funding_grant",
                resource_id=str(_attr(row, "id")),
                reason=reason_text,
                metadata={
                    "operation_key": key,
                    "resource_key": str(_attr(row, "resource_key")),
                    "purpose": str(_attr(row, "purpose")),
                },
            )
            await session.commit()
            await session.refresh(row)
            return _grant_to_domain(row)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    async def list_expenses_between(
        self,
        start: datetime,
        end: datetime,
        *,
        limit: int = 500,
        offset: int = 0,
    ) -> list[OperatorExpense]:
        """Bounded, stably ordered expenses whose ``occurred_at`` is in [start, end)."""
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            raise FundingValidationError("expense range bounds must be datetimes")
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or start.utcoffset() is None
            or end.utcoffset() is None
        ):
            raise FundingValidationError("expense range bounds must be timezone-aware")
        if start >= end:
            raise FundingValidationError("expense range start must precede end")
        if type(limit) is not int or not 0 < limit <= MAX_EXPENSE_PAGE:
            raise FundingValidationError(f"limit must be between 1 and {MAX_EXPENSE_PAGE}")
        if type(offset) is not int or offset < 0:
            raise FundingValidationError("offset must not be negative")
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_ExpenseModel)
                        .where(
                            _ExpenseModel.occurred_at >= to_db_utc(start),
                            _ExpenseModel.occurred_at < to_db_utc(end),
                        )
                        .order_by(_ExpenseModel.occurred_at, _ExpenseModel.id)
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_expense_to_domain(row) for row in rows]
