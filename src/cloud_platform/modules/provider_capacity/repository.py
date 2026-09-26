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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ProviderAccountCapacity as _ProviderAccountCapacityModel
from cloud_platform.db.base import (
    ProviderAccountCapacityEvent as _ProviderAccountCapacityEventModel,
)
from cloud_platform.modules.provider_capacity.domain import (
    DEFAULT_LIMIT_TTL_SECONDS,
    MIN_RECOVERY_DELAY_SECONDS,
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

#: Evidence kinds that prove a positive decision (recovery PROVEN, either by
#: an operator override or by a real accepted canary order). Newer positive
#: evidence outranks an older historical refusal during reconciliation.
_CLEARED_KINDS = (
    CapacityEventKind.CLEARED.value,
    CapacityEventKind.RECOVERED.value,
)


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
        baseline_instance_count=_attr(row, "baseline_instance_count"),
        baseline_instance_ids_hash=_attr(row, "baseline_instance_ids_hash"),
        baseline_observed_at=_attr(row, "baseline_observed_at"),
        recovery_attempts=int(_attr(row, "recovery_attempts") or 0),
        last_recovery_attempt_at=_attr(row, "last_recovery_attempt_at"),
        next_recovery_attempt_at=_attr(row, "next_recovery_attempt_at"),
        canary_lease_expires_at=_attr(row, "canary_lease_expires_at"),
        canary_lease_ref=_attr(row, "canary_lease_ref"),
        outage_notified_at=_attr(row, "outage_notified_at"),
        last_reminder_at=_attr(row, "last_reminder_at"),
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
        # A refusal that arrives while the account was HEALTHY starts a NEW
        # outage: the previous baseline, recovery counter and notification
        # bookkeeping belong to the previous incident and are reset. A refusal
        # on an already-blocked account keeps that context.
        fresh_outage = model.state == AccountCapacityState.HEALTHY.value
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
                "baseline_instance_count": case(
                    (fresh_outage, None), else_=model.baseline_instance_count
                ),
                "baseline_instance_ids_hash": case(
                    (fresh_outage, None), else_=model.baseline_instance_ids_hash
                ),
                "baseline_observed_at": case(
                    (fresh_outage, None), else_=model.baseline_observed_at
                ),
                "recovery_attempts": case((fresh_outage, 0), else_=model.recovery_attempts),
                "canary_lease_ref": case((fresh_outage, None), else_=model.canary_lease_ref),
                "canary_lease_expires_at": case(
                    (fresh_outage, None), else_=model.canary_lease_expires_at
                ),
                "outage_notified_at": case((fresh_outage, None), else_=model.outage_notified_at),
                "last_reminder_at": case((fresh_outage, None), else_=model.last_reminder_at),
                "next_recovery_attempt_at": case(
                    (fresh_outage, None), else_=model.next_recovery_attempt_at
                ),
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

    # -- automatic canary recovery -----------------------------------------

    async def record_inventory_census(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        instance_count: int,
        ids_hash: str,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Read-only inventory evidence for one blocked account.

        The FIRST census after a refusal becomes the baseline (what the account
        held when the limit was learned). A LATER census below that baseline
        means an instance was freed: the next recovery window is brought
        forward to ``now`` so the canary can be spent immediately instead of
        waiting for the backoff.
        """
        if isinstance(instance_count, bool) or not isinstance(instance_count, int):
            raise ValueError("instance_count must be an integer")
        if instance_count < 0:
            raise ValueError("instance_count must be >= 0")
        if not str(ids_hash or "").strip():
            raise ValueError("ids_hash must not be empty")
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(model)
                        .where(
                            model.provider_key == provider_key,
                            model.credential_account_id == credential_account_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                await session.rollback()
                return None
            record = _capacity_from_row(row)
            if not record.is_limit_reached(now=reference):
                await session.rollback()
                return record
            updated = record
            if record.baseline_instance_count is None:
                updated = record.with_baseline(
                    instance_count=instance_count, ids_hash=str(ids_hash), now=reference
                )
            elif instance_count < record.baseline_instance_count:
                # An instance was freed: the scheduled backoff is no longer the
                # best estimate of when capacity may exist. Open the window now.
                updated = replace(record, next_recovery_attempt_at=reference)
            target: Any = row
            target.baseline_instance_count = updated.baseline_instance_count
            target.baseline_instance_ids_hash = updated.baseline_instance_ids_hash
            target.baseline_observed_at = updated.baseline_observed_at
            target.next_recovery_attempt_at = updated.next_recovery_attempt_at
            await session.commit()
        return updated.settled(now=reference)

    async def bring_forward_recovery(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """An app-owned instance was removed: evaluate the account immediately.

        The delete saga calls this with a LOCAL fact (no provider call). The
        next controller pass then sees the inventory decrease and opens the
        canary window without waiting out the backoff. Only a blocked account
        with a later/absent schedule is changed; a healthy account, an open
        window or an in-flight canary is left alone.
        """
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
                model.state.in_(
                    (
                        AccountCapacityState.LIMIT_REACHED.value,
                        AccountCapacityState.UNKNOWN_AFTER_LIMIT.value,
                    )
                ),
                or_(
                    model.next_recovery_attempt_at.is_(None),
                    model.next_recovery_attempt_at > reference,
                ),
            )
            .values(next_recovery_attempt_at=reference)
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
        return bool(cast(Any, result).rowcount)

    async def schedule_recovery(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        delay_seconds: int,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Schedule the next canary window for a blocked account.

        A no-op when the account is eligible, already scheduled, or has an
        attempt in flight: the earliest existing schedule always wins, so two
        workers can never fight over the delay.
        """
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int):
            raise ValueError("delay_seconds must be an integer")
        if delay_seconds < MIN_RECOVERY_DELAY_SECONDS:
            raise ValueError(f"delay_seconds must be >= {MIN_RECOVERY_DELAY_SECONDS}")
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(model)
                        .where(
                            model.provider_key == provider_key,
                            model.credential_account_id == credential_account_id,
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                await session.rollback()
                return None
            record = _capacity_from_row(row)
            if (
                not record.is_limit_reached(now=reference)
                or record.next_recovery_attempt_at is not None
                or record.canary_lease_held(now=reference)
            ):
                await session.rollback()
                return record
            scheduled = record.with_recovery_scheduled(delay_seconds=delay_seconds, now=reference)
            target: Any = row
            target.next_recovery_attempt_at = scheduled.next_recovery_attempt_at
            await session.commit()
        return scheduled.settled(now=reference)

    async def open_recovery_window(
        self, provider_key: str, credential_account_id: str, *, now: datetime | None = None
    ) -> AccountCapacity | None:
        """Open the canary window when the schedule is due (single winner).

        ``None`` when the account is eligible, still cooling, has no schedule
        or a canary already in flight — the window is never opened by accident.
        """
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
                model.state.in_(
                    (
                        AccountCapacityState.LIMIT_REACHED.value,
                        AccountCapacityState.UNKNOWN_AFTER_LIMIT.value,
                    )
                ),
                model.next_recovery_attempt_at.isnot(None),
                model.next_recovery_attempt_at <= reference,
                or_(
                    model.canary_lease_expires_at.is_(None),
                    model.canary_lease_expires_at <= reference,
                ),
            )
            .values(
                state=AccountCapacityState.RECOVERY_CANDIDATE.value,
                next_recovery_attempt_at=None,
                canary_lease_ref=None,
                canary_lease_expires_at=None,
            )
            .returning(model)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).scalars().first()
            if row is None:
                await session.rollback()
                return None
            await session.commit()
        return _capacity_from_row(row)

    async def begin_canary_attempt(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """Acquire the DURABLE single-canary lease for one real order.

        One conditional UPDATE is the whole serialization: exactly one caller
        can move a free lease to its own reference, so two customer orders can
        never both become the canary. The evidence row is appended in the same
        transaction, keyed by the attempt reference, so a replayed attempt
        never records twice.
        """
        if not str(ref or "").strip():
            raise ValueError("ref must not be empty")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ValueError("lease_seconds must be an integer")
        if lease_seconds < MIN_RECOVERY_DELAY_SECONDS:
            raise ValueError(f"lease_seconds must be >= {MIN_RECOVERY_DELAY_SECONDS}")
        reference = _aware(now)
        expires_at = reference + timedelta(seconds=lease_seconds)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
                model.state == AccountCapacityState.RECOVERY_CANDIDATE.value,
                or_(
                    model.canary_lease_expires_at.is_(None),
                    model.canary_lease_expires_at <= reference,
                ),
            )
            .values(
                canary_lease_ref=str(ref),
                canary_lease_expires_at=expires_at,
                last_recovery_attempt_at=reference,
            )
            .returning(model)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).scalars().first()
            if row is None:
                await session.rollback()
                return None
            await session.execute(
                pg_insert(_ProviderAccountCapacityEventModel)
                .values(
                    **_event_values(
                        provider_key=provider_key,
                        credential_account_id=credential_account_id,
                        kind=CapacityEventKind.RECOVERY_ATTEMPT,
                        state=AccountCapacityState.RECOVERY_CANDIDATE,
                        observation=CapacityObservation(),
                        source_ref=f"canary:{ref}",
                        observed_at=reference,
                    )
                )
                .on_conflict_do_nothing(
                    index_elements=list(_EVENT_INDEX_ELEMENTS),
                    index_where=_ProviderAccountCapacityEventModel.source_ref.isnot(None),
                )
            )
            await session.commit()
        return _capacity_from_row(row)

    async def release_canary_lease(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Release a lease (the matching attempt when ``ref`` is supplied)."""
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        statement = update(model).where(
            model.provider_key == provider_key,
            model.credential_account_id == credential_account_id,
            model.canary_lease_ref.isnot(None),
            model.canary_lease_expires_at.isnot(None),
            model.canary_lease_expires_at > reference,
        )
        if ref is not None:
            statement = statement.where(model.canary_lease_ref == str(ref))
        statement = statement.values(canary_lease_ref=None, canary_lease_expires_at=None)
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
        return bool(cast(Any, result).rowcount)

    async def record_canary_refusal(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str,
        observation: CapacityObservation,
        delay_seconds: int,
        ttl_seconds: int = DEFAULT_LIMIT_TTL_SECONDS,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """A canary was refused again: back off, stay unpublished.

        Guarded by the lease reference, so replaying the same attempt is a
        no-op (``None``) instead of a second attempt count and a second log
        line. The canonical refusal evidence is refreshed in the same
        transaction.
        """
        if not str(ref or "").strip():
            raise ValueError("ref must not be empty")
        ttl = validate_limit_ttl(ttl_seconds)
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, int):
            raise ValueError("delay_seconds must be an integer")
        if delay_seconds < MIN_RECOVERY_DELAY_SECONDS:
            raise ValueError(f"delay_seconds must be >= {MIN_RECOVERY_DELAY_SECONDS}")
        reference = _aware(now)
        expires_at = reference + timedelta(seconds=ttl)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
                model.state == AccountCapacityState.RECOVERY_CANDIDATE.value,
                model.canary_lease_ref == str(ref),
            )
            .values(
                state=AccountCapacityState.LIMIT_REACHED.value,
                error_code=func.coalesce(observation.error_code, model.error_code),
                correlation_id=func.coalesce(observation.correlation_id, model.correlation_id),
                location_id=func.coalesce(observation.location_id, model.location_id),
                product_id=func.coalesce(observation.product_id, model.product_id),
                observations=model.observations + 1,
                observed_at=reference,
                expires_at=expires_at,
                recovery_attempts=model.recovery_attempts + 1,
                last_recovery_attempt_at=reference,
                next_recovery_attempt_at=reference + timedelta(seconds=delay_seconds),
                canary_lease_ref=None,
                canary_lease_expires_at=None,
            )
            .returning(model)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).scalars().first()
            if row is None:
                await session.rollback()
                return None
            await session.execute(
                pg_insert(_ProviderAccountCapacityEventModel)
                .values(
                    **_event_values(
                        provider_key=provider_key,
                        credential_account_id=credential_account_id,
                        kind=CapacityEventKind.REFUSAL,
                        state=AccountCapacityState.LIMIT_REACHED,
                        observation=observation,
                        source_ref=f"canary:{ref}",
                        observed_at=reference,
                        expires_at=expires_at,
                    )
                )
                .on_conflict_do_nothing(
                    index_elements=list(_EVENT_INDEX_ELEMENTS),
                    index_where=_ProviderAccountCapacityEventModel.source_ref.isnot(None),
                )
            )
            await session.commit()
        return _capacity_from_row(row)

    async def record_recovery_proven(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        ref: str,
        now: datetime | None = None,
    ) -> AccountCapacity | None:
        """A canary order was ACCEPTED: eligibility is proven, not guessed."""
        if not str(ref or "").strip():
            raise ValueError("ref must not be empty")
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
                model.canary_lease_ref == str(ref),
            )
            .values(
                state=AccountCapacityState.HEALTHY.value,
                expires_at=None,
                observed_at=reference,
                recovery_attempts=0,
                next_recovery_attempt_at=None,
                canary_lease_ref=None,
                canary_lease_expires_at=None,
                outage_notified_at=None,
                last_reminder_at=None,
            )
            .returning(model)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).scalars().first()
            if row is None:
                await session.rollback()
                return await self.get(provider_key, credential_account_id)
            await session.execute(
                pg_insert(_ProviderAccountCapacityEventModel)
                .values(
                    **_event_values(
                        provider_key=provider_key,
                        credential_account_id=credential_account_id,
                        kind=CapacityEventKind.RECOVERED,
                        state=AccountCapacityState.HEALTHY,
                        observation=CapacityObservation(),
                        source_ref=f"canary:{ref}",
                        observed_at=reference,
                    )
                )
                .on_conflict_do_nothing(
                    index_elements=list(_EVENT_INDEX_ELEMENTS),
                    index_where=_ProviderAccountCapacityEventModel.source_ref.isnot(None),
                )
            )
            await session.commit()
        return _capacity_from_row(row)

    async def mark_outage_notified(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Remember that this outage's operator card was enqueued."""
        reference = _aware(now)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
                model.outage_notified_at.is_(None),
            )
            .values(outage_notified_at=reference)
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
        return bool(cast(Any, result).rowcount)

    async def mark_reminder_sent(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        sent_at: datetime | None = None,
    ) -> bool:
        """Remember the reminder instant (the outbox key is the real dedupe)."""
        reference = _aware(sent_at)
        model = _ProviderAccountCapacityModel
        statement = (
            update(model)
            .where(
                model.provider_key == provider_key,
                model.credential_account_id == credential_account_id,
            )
            .values(last_reminder_at=reference)
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            await session.commit()
        return bool(cast(Any, result).rowcount)

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
                    # The operator override is an explicit reset of the
                    # automated recovery state: attempts, schedule, lease and
                    # outage bookkeeping all start from a clean slate.
                    "recovery_attempts": 0,
                    "next_recovery_attempt_at": None,
                    "canary_lease_ref": None,
                    "canary_lease_expires_at": None,
                    "outage_notified_at": None,
                    "last_reminder_at": None,
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
