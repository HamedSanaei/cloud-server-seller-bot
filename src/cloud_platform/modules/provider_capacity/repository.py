"""SQLAlchemy adapter for durable credential-account capacity rows.

Same conventions as the other repositories — an injected session factory, no
credential material anywhere in the row (an error code, a correlation id, a
location and a product id only) — with ONE deliberate difference: the mutating
methods are single-transaction PostgreSQL writes.

That is not a micro-optimisation. Several workers can observe the same
definitive account refusal within the same second (the hourly worker plus a
retry of the sync loop), and a select-then-insert would make them race the
``(provider_key, credential_account_id)`` unique constraint: one transaction
would win and the others would raise a unique violation instead of simply
counting a second observation. An ``INSERT ... ON CONFLICT DO UPDATE`` makes
the whole transition atomic — one row per account, every observation counted,
no duplicate-key error — which real PostgreSQL is the only thing that can
prove.

Reads SETTLE the record: a ``limit_reached`` row whose cooling window elapsed
is returned as ``unknown_after_limit``. Expiry removes the freshness of the
refusal, never the fact that recovery is unproven, so no caller can observe
"the TTL passed, therefore eligible".

A capacity write is NEVER allowed to break the operation it describes: the
callers in the hourly service treat a failure here as "unknown capacity" and
still fail the provider operation itself. That is why every method is narrow,
idempotent and returns the resulting record.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ProviderAccountCapacity as _ProviderAccountCapacityModel
from cloud_platform.db.base import (
    ProviderAccountCapacityEvent as _ProviderAccountCapacityEventModel,
)
from cloud_platform.modules.provider_capacity.domain import (
    DEFAULT_LIMIT_TTL_SECONDS,
    AccountCapacity,
    AccountCapacityState,
    CapacityEvent,
    CapacityEventKind,
    CapacityObservation,
    HistoricalCapacityEvidence,
    validate_limit_ttl,
)

__all__ = ["SqlAlchemyAccountCapacityRepository"]

#: The database-level unique identity of a capacity row; the upsert target.
_CONSTRAINT = "uq_provider_account_capacity_account"

#: Columns of the partial unique index that makes the historical
#: reconciliation idempotent (one evidence row per provider operation).
_EVENT_INDEX_ELEMENTS = ("provider_key", "credential_account_id", "kind", "source_ref")

#: Evidence kinds that prove an operator/positive-proof decision was made.
_CLEARED_KINDS = (CapacityEventKind.CLEARED.value,)


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _state(raw: str) -> AccountCapacityState:
    """Map a stored state string; unknown values never read as healthy."""
    try:
        return AccountCapacityState(str(raw))
    except ValueError:
        # Forward compatibility: an unknown state from a newer release must
        # never read as "healthy" — unknown means "do not use for new orders".
        return AccountCapacityState.UNKNOWN_AFTER_LIMIT


def _capacity_from_row(row: Any) -> AccountCapacity:
    """Map one provider_account_capacity row onto the SETTLED domain record."""
    return AccountCapacity(
        provider_key=str(_attr(row, "provider_key")),
        credential_account_id=str(_attr(row, "credential_account_id")),
        state=_state(_attr(row, "state")),
        error_code=_attr(row, "error_code"),
        correlation_id=_attr(row, "correlation_id"),
        location_id=_attr(row, "location_id"),
        product_id=_attr(row, "product_id"),
        observations=int(_attr(row, "observations") or 0),
        observed_at=_attr(row, "observed_at"),
        expires_at=_attr(row, "expires_at"),
    ).settled()


def _event_from_row(row: Any) -> CapacityEvent:
    return CapacityEvent(
        provider_key=str(_attr(row, "provider_key")),
        credential_account_id=str(_attr(row, "credential_account_id")),
        kind=CapacityEventKind(str(_attr(row, "kind"))),
        state=_state(_attr(row, "state")),
        error_code=_attr(row, "error_code"),
        correlation_id=_attr(row, "correlation_id"),
        location_id=_attr(row, "location_id"),
        product_id=_attr(row, "product_id"),
        source_ref=_attr(row, "source_ref"),
        observed_at=_attr(row, "observed_at"),
        expires_at=_attr(row, "expires_at"),
        created_at=_attr(row, "created_at"),
    )


def _event_values(
    *,
    provider_key: str,
    credential_account_id: str,
    kind: CapacityEventKind,
    state: AccountCapacityState,
    observation: CapacityObservation,
    source_ref: str | None = None,
    observed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "provider_key": provider_key,
        "credential_account_id": credential_account_id,
        "kind": kind.value,
        "state": state.value,
        "error_code": observation.error_code,
        "correlation_id": observation.correlation_id,
        "location_id": observation.location_id,
        "product_id": observation.product_id,
        "source_ref": source_ref,
        "observed_at": observed_at,
        "expires_at": expires_at,
    }


class SqlAlchemyAccountCapacityRepository:
    """Durable per-account capacity knowledge for credential-scoped providers."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def get(self, provider_key: str, credential_account_id: str) -> AccountCapacity | None:
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_ProviderAccountCapacityModel).where(
                            _ProviderAccountCapacityModel.provider_key == provider_key,
                            _ProviderAccountCapacityModel.credential_account_id
                            == credential_account_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
        return None if row is None else _capacity_from_row(row)

    async def list_for_provider(self, provider_key: str) -> tuple[AccountCapacity, ...]:
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_ProviderAccountCapacityModel)
                        .where(_ProviderAccountCapacityModel.provider_key == provider_key)
                        .order_by(_ProviderAccountCapacityModel.credential_account_id)
                    )
                )
                .scalars()
                .all()
            )
        return tuple(_capacity_from_row(row) for row in rows)

    async def limit_reached_accounts(
        self, provider_key: str, *, now: datetime | None = None
    ) -> frozenset[str]:
        """Accounts that must not receive NEW orders (settled, not just fresh)."""
        records = await self.list_for_provider(provider_key)
        return frozenset(
            record.credential_account_id for record in records if record.is_limit_reached(now=now)
        )

    async def record_limit_reached(
        self,
        *,
        provider_key: str,
        credential_account_id: str,
        observation: CapacityObservation,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AccountCapacity:
        """Upsert one definitive LIVE capacity refusal (atomic per account).

        The transition matches ``AccountCapacity.with_limit_reached`` exactly —
        a sliding window from ``now``, accumulated observations, and evidence
        carried forward when this refusal omitted a field — but it is evaluated
        BY THE DATABASE inside the conflicting row's lock, so N concurrent
        refusals end as ONE row with N observations. The evidence row is
        appended in the SAME transaction, so the history can never disagree
        with the row it explains.
        """
        ttl = validate_limit_ttl(ttl_seconds)
        observed_at = _aware(now)
        expires_at = observed_at + timedelta(seconds=ttl)
        model = _ProviderAccountCapacityModel
        insertion = pg_insert(model).values(
            provider_key=provider_key,
            credential_account_id=credential_account_id,
            state=AccountCapacityState.LIMIT_REACHED.value,
            error_code=observation.error_code,
            correlation_id=observation.correlation_id,
            location_id=observation.location_id,
            product_id=observation.product_id,
            observations=1,
            observed_at=observed_at,
            expires_at=expires_at,
        )
        # An observation that omits a field never erases evidence an earlier
        # refusal recorded, so each carried-forward column coalesces onto the
        # row the conflict found.
        excluded = insertion.excluded
        statement = insertion.on_conflict_do_update(
            constraint=_CONSTRAINT,
            set_={
                "state": AccountCapacityState.LIMIT_REACHED.value,
                "error_code": func.coalesce(excluded.error_code, model.error_code),
                "correlation_id": func.coalesce(excluded.correlation_id, model.correlation_id),
                "location_id": func.coalesce(excluded.location_id, model.location_id),
                "product_id": func.coalesce(excluded.product_id, model.product_id),
                "observations": model.observations + 1,
                "observed_at": observed_at,
                "expires_at": expires_at,
            },
        ).returning(model)
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(_ProviderAccountCapacityEventModel).values(
                    **_event_values(
                        provider_key=provider_key,
                        credential_account_id=credential_account_id,
                        kind=CapacityEventKind.REFUSAL,
                        state=AccountCapacityState.LIMIT_REACHED,
                        observation=observation,
                        observed_at=observed_at,
                        expires_at=expires_at,
                    )
                )
            )
            row = (await session.execute(statement)).scalars().one()
            await session.commit()
        return _capacity_from_row(row)

    async def record_historical_evidence(
        self,
        evidence: HistoricalCapacityEvidence,
        *,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Reconcile ONE past provider failure into durable capacity knowledge.

        Exactly-once by construction: the evidence row is keyed by the provider
        operation (``source_ref``), so replaying the reconciliation appends
        nothing and returns ``None``. The historical incident keeps its own
        timeline, so an old refusal never pretends to be a fresh window — it
        lands as ``unknown_after_limit`` the moment its window is already over.

        An operator clear (or any positive proof) recorded AFTER the incident is
        newer evidence and wins: the operation is logged, but eligibility is not
        revoked by history.
        """
        ttl = validate_limit_ttl(ttl_seconds)
        reference = _aware(now)
        observed_at = _aware(evidence.observed_at) if evidence.observed_at else reference
        expires_at = observed_at + timedelta(seconds=ttl)
        model = _ProviderAccountCapacityModel
        events = _ProviderAccountCapacityEventModel
        async with self._session_factory() as session:
            cleared_at = (
                await session.execute(
                    select(func.max(events.created_at)).where(
                        events.provider_key == evidence.provider_key,
                        events.credential_account_id == evidence.credential_account_id,
                        events.kind.in_(_CLEARED_KINDS),
                    )
                )
            ).scalar()
            current_row = (
                (
                    await session.execute(
                        select(model)
                        .where(
                            model.provider_key == evidence.provider_key,
                            model.credential_account_id == evidence.credential_account_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            current = None if current_row is None else _capacity_from_row(current_row)
            cleared_wins = bool(
                cleared_at is not None
                and _aware(cleared_at) >= observed_at
                and current is not None
                and current.state is AccountCapacityState.HEALTHY
            )
            resulting_state = (
                AccountCapacityState.HEALTHY
                if cleared_wins
                else (
                    AccountCapacityState.LIMIT_REACHED
                    if observed_at + timedelta(seconds=ttl) > reference
                    else AccountCapacityState.UNKNOWN_AFTER_LIMIT
                )
            )
            inserted = (
                (
                    await session.execute(
                        pg_insert(events)
                        .values(
                            **_event_values(
                                provider_key=evidence.provider_key,
                                credential_account_id=evidence.credential_account_id,
                                kind=CapacityEventKind.BACKFILL,
                                state=resulting_state,
                                observation=evidence.as_observation(),
                                source_ref=evidence.source_ref,
                                observed_at=observed_at,
                                expires_at=expires_at,
                            )
                        )
                        .on_conflict_do_nothing(
                            index_elements=list(_EVENT_INDEX_ELEMENTS),
                            index_where=events.source_ref.isnot(None),
                        )
                        .returning(events.id)
                    )
                )
                .scalars()
                .first()
            )
            if inserted is None:
                # This exact failure was reconciled before: nothing to change.
                await session.rollback()
                return None
            if cleared_wins:
                await session.commit()
                return current
            if current_row is None:
                await session.execute(
                    pg_insert(model).values(
                        provider_key=evidence.provider_key,
                        credential_account_id=evidence.credential_account_id,
                        state=resulting_state.value,
                        error_code=evidence.error_code,
                        correlation_id=evidence.correlation_id,
                        observations=1,
                        observed_at=observed_at,
                        expires_at=expires_at,
                    )
                )
            elif current is not None:
                updated = current.with_limit_reached(
                    observation=evidence.as_observation(),
                    ttl_seconds=ttl,
                    observed_at=observed_at,
                )
                # Legacy-style Column attributes: the boundary is untyped.
                row_any: Any = current_row
                row_any.state = updated.state.value
                row_any.error_code = updated.error_code
                row_any.correlation_id = updated.correlation_id
                row_any.location_id = updated.location_id
                row_any.product_id = updated.product_id
                row_any.observations = updated.observations
                row_any.observed_at = updated.observed_at
                row_any.expires_at = updated.expires_at
            row = (
                (
                    await session.execute(
                        select(model).where(
                            model.provider_key == evidence.provider_key,
                            model.credential_account_id == evidence.credential_account_id,
                        )
                    )
                )
                .scalars()
                .one()
            )
            await session.commit()
        return _capacity_from_row(row)

    async def record_healthy(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
        reason: str | None = None,
    ) -> AccountCapacity:
        """Clear the limit state after proven success or an operator re-probe.

        Evidence of the last refusal is KEPT (only eligibility changes): "this
        account once refused a create" is useful context for the doctor, and
        deleting it would make an operator's re-probe indistinguishable from an
        account that never had a problem. The clear itself is appended to the
        evidence log, which is also what makes it outrank an older historical
        refusal during a reconciliation.
        """
        observed_at = _aware(now)
        model = _ProviderAccountCapacityModel
        statement = (
            pg_insert(model)
            .values(
                provider_key=provider_key,
                credential_account_id=credential_account_id,
                state=AccountCapacityState.HEALTHY.value,
                observations=0,
                observed_at=observed_at,
            )
            .on_conflict_do_update(
                constraint=_CONSTRAINT,
                set_={
                    "state": AccountCapacityState.HEALTHY.value,
                    "expires_at": None,
                    "observed_at": observed_at,
                },
            )
            .returning(model)
        )
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(_ProviderAccountCapacityEventModel).values(
                    **_event_values(
                        provider_key=provider_key,
                        credential_account_id=credential_account_id,
                        kind=CapacityEventKind.CLEARED,
                        state=AccountCapacityState.HEALTHY,
                        observation=CapacityObservation(),
                        observed_at=observed_at,
                    )
                )
            )
            row = (await session.execute(statement)).scalars().one()
            await session.commit()
        return _capacity_from_row(row)

    async def settle_expired(
        self, provider_key: str, *, now: datetime | None = None
    ) -> tuple[str, ...]:
        """Persist ``limit_reached`` -> ``unknown_after_limit`` for stale rows.

        Reads already settle, so this is housekeeping that keeps the STORED row
        equal to what the platform believes; it never makes an account eligible.
        """
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.state == AccountCapacityState.LIMIT_REACHED.value,
                model.expires_at.isnot(None),
                model.expires_at <= reference,
            )
            .values(state=AccountCapacityState.UNKNOWN_AFTER_LIMIT.value)
            .returning(model.credential_account_id)
        )
        async with self._session_factory() as session:
            accounts = tuple(
                str(account) for account in (await session.execute(statement)).scalars().all()
            )
            await session.commit()
        return accounts

    async def list_events(
        self,
        provider_key: str,
        *,
        credential_account_id: str | None = None,
        limit: int = 20,
    ) -> tuple[CapacityEvent, ...]:
        """The append-only evidence history, newest first."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        model = _ProviderAccountCapacityEventModel
        statement = select(model).where(model.provider_key == provider_key)
        if credential_account_id is not None:
            statement = statement.where(model.credential_account_id == credential_account_id)
        statement = statement.order_by(model.created_at.desc(), model.id.desc()).limit(limit)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        return tuple(_event_from_row(row) for row in rows)

    async def clear(self, provider_key: str, credential_account_id: str) -> bool:
        """Delete the record entirely (operator "forget this signal")."""
        model = _ProviderAccountCapacityModel
        statement = (
            delete(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
            )
            .returning(model.id)
        )
        async with self._session_factory() as session:
            removed = (await session.execute(statement)).scalars().one_or_none()
            await session.commit()
        return removed is not None


def _aware(value: datetime | None) -> datetime:
    resolved = value or datetime.now(UTC)
    return resolved if resolved.tzinfo is not None else resolved.replace(tzinfo=UTC)
