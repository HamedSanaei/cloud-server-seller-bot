"""SQLAlchemy adapter for durable credential-account capacity rows.

Same conventions as the other repositories — an injected session factory, no
credential material anywhere in the row (an error code, a correlation id, a
location and a product id only) — with ONE deliberate difference: the mutating
methods are single-statement PostgreSQL upserts.

That is not a micro-optimisation. Several workers can observe the same
definitive account refusal within the same second (the hourly worker plus a
retry of the sync loop), and a select-then-insert would make them race the
``(provider_key, credential_account_id)`` unique constraint: one transaction
would win and the others would raise a unique violation instead of simply
counting a second observation. An ``INSERT ... ON CONFLICT DO UPDATE`` makes
the whole transition atomic — one row per account, every observation counted,
no duplicate-key error — which real PostgreSQL is the only thing that can
prove.

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

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ProviderAccountCapacity as _ProviderAccountCapacityModel
from cloud_platform.modules.provider_capacity.domain import (
    DEFAULT_LIMIT_TTL_SECONDS,
    AccountCapacity,
    AccountCapacityState,
    CapacityObservation,
    validate_limit_ttl,
)

__all__ = ["SqlAlchemyAccountCapacityRepository"]

#: The database-level unique identity of a capacity row; the upsert target.
_CONSTRAINT = "uq_provider_account_capacity_account"


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _capacity_from_row(row: Any) -> AccountCapacity:
    """Map one provider_account_capacity row onto the domain record."""
    raw_state = str(_attr(row, "state"))
    try:
        state = AccountCapacityState(raw_state)
    except ValueError:
        # Forward compatibility: an unknown state from a newer release must
        # never read as "healthy" — unknown means "do not use for new orders".
        state = AccountCapacityState.LIMIT_REACHED
    return AccountCapacity(
        provider_key=str(_attr(row, "provider_key")),
        credential_account_id=str(_attr(row, "credential_account_id")),
        state=state,
        error_code=_attr(row, "error_code"),
        correlation_id=_attr(row, "correlation_id"),
        location_id=_attr(row, "location_id"),
        product_id=_attr(row, "product_id"),
        observations=int(_attr(row, "observations") or 0),
        observed_at=_attr(row, "observed_at"),
        expires_at=_attr(row, "expires_at"),
    )


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
        """Accounts currently out of capacity (expired windows excluded)."""
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
        """Upsert one definitive capacity refusal (exactly-once per account).

        The transition matches ``AccountCapacity.with_limit_reached`` exactly —
        a sliding window from ``now``, accumulated observations, and evidence
        carried forward when this refusal omitted a field — but it is evaluated
        BY THE DATABASE inside the conflicting row's lock, so N concurrent
        refusals end as ONE row with N observations.
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
            row = (await session.execute(statement)).scalars().one()
            await session.commit()
        return _capacity_from_row(row)

    async def record_healthy(
        self,
        provider_key: str,
        credential_account_id: str,
        *,
        now: datetime | None = None,
    ) -> AccountCapacity:
        """Clear the limit state after proven success or an operator re-probe.

        Evidence of the last refusal is KEPT (only eligibility changes): "this
        account once refused a create" is useful context for the doctor, and
        deleting it would make an operator's re-probe indistinguishable from an
        account that never had a problem.
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
            row = (await session.execute(statement)).scalars().one()
            await session.commit()
        return _capacity_from_row(row)

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
