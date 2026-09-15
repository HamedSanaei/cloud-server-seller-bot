"""SQLAlchemy adapter for durable credential-account routing rows.

Same conventions as the other repositories: an injected session factory, a
select-then-mutate upsert against the ``(provider_key, credential_account_id,
location_id)`` unique constraint, and no credential material anywhere in the
row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cloud_platform.db.base import ProviderRoute as _ProviderRouteModel
from cloud_platform.modules.provider_routes.domain import (
    ProviderRoute,
    RouteObservation,
    RouteState,
)
from cloud_platform.providers.routing import CredentialAccountState


def _attr(row: Any, name: str) -> Any:
    """Read a legacy-style Column attribute; typed as Any at the boundary."""
    return getattr(row, name)


def _route_from_row(row: Any) -> ProviderRoute:
    """Map one provider_routes row onto the domain route."""
    raw_state = str(_attr(row, "state"))
    try:
        state = RouteState(raw_state)
    except ValueError:
        # Forward compatibility: an unknown state from a newer release means
        # "not currently proven available", never "available".
        state = RouteState.TRANSIENT_UNKNOWN
    raw_account_state = str(_attr(row, "account_state"))
    try:
        account_state = CredentialAccountState(raw_account_state)
    except ValueError:
        account_state = CredentialAccountState.DISABLED
    products = _attr(row, "product_ids") or []
    return ProviderRoute(
        provider_key=str(_attr(row, "provider_key")),
        credential_account_id=str(_attr(row, "credential_account_id")),
        location_id=str(_attr(row, "location_id")),
        state=state,
        priority=int(_attr(row, "priority") or 0),
        product_ids=tuple(str(product) for product in products),
        last_checked_at=_attr(row, "last_checked_at"),
        last_success_at=_attr(row, "last_success_at"),
        last_error_class=_attr(row, "last_error_class"),
        account_state=account_state,
    )


class SqlAlchemyProviderRouteRepository:
    """Durable routing knowledge for credential-account-scoped providers."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    ) -> None:
        self._session_factory = session_factory

    async def upsert(
        self,
        route: ProviderRoute,
        *,
        products_fresh: bool = False,
    ) -> None:
        """Insert or update ONE (provider, account, location) observation.

        ``products_fresh`` says whether ``route.product_ids`` is this run's
        product-level evidence for the account. It is only replaced when true,
        so a location-level observation never erases the product inventory a
        previous probe recorded.
        """
        async with self._session_factory() as session:
            row = (
                (
                    await session.execute(
                        select(_ProviderRouteModel).where(
                            _ProviderRouteModel.provider_key == route.provider_key,
                            _ProviderRouteModel.credential_account_id
                            == route.credential_account_id,
                            _ProviderRouteModel.location_id == route.location_id,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is None:
                row = _ProviderRouteModel(
                    provider_key=route.provider_key,
                    credential_account_id=route.credential_account_id,
                    location_id=route.location_id,
                    state=route.state.value,
                    account_state=route.account_state.value,
                    priority=route.priority,
                    product_ids=list(route.product_ids),
                    last_error_class=route.last_error_class,
                    last_checked_at=route.last_checked_at,
                    last_success_at=route.last_success_at,
                )
                session.add(row)
            else:
                target: Any = row
                target.state = route.state.value
                target.account_state = route.account_state.value
                target.priority = route.priority
                target.last_error_class = route.last_error_class
                target.last_checked_at = route.last_checked_at
                if route.last_success_at is not None:
                    target.last_success_at = route.last_success_at
                if products_fresh:
                    target.product_ids = list(route.product_ids)
            await session.commit()

    async def upsert_observations(
        self,
        *,
        provider_key: str,
        observations: Iterable[RouteObservation],
        priority_of: Callable[[str], int],
        account_state_of: Callable[[str], CredentialAccountState],
        at: datetime | None = None,
    ) -> int:
        """Persist one sync run's observations (idempotent per route)."""
        checked_at = at or datetime.now(UTC)
        written = 0
        for observation in observations:
            state = observation.state
            route = ProviderRoute(
                provider_key=provider_key,
                credential_account_id=observation.credential_account_id,
                location_id=observation.location_id,
                state=state,
                priority=priority_of(observation.credential_account_id),
                product_ids=observation.product_ids,
                last_checked_at=checked_at,
                last_success_at=checked_at if observation.succeeded else None,
                last_error_class=observation.error_class,
                account_state=account_state_of(observation.credential_account_id),
            )
            await self.upsert(route, products_fresh=state is RouteState.ELIGIBLE_AVAILABLE)
            written += 1
        return written

    async def list_for_provider(self, provider_key: str) -> list[ProviderRoute]:
        """Every route observation of one provider (deterministic order)."""
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_ProviderRouteModel)
                        .where(_ProviderRouteModel.provider_key == provider_key)
                        .order_by(
                            _ProviderRouteModel.location_id,
                            _ProviderRouteModel.priority,
                            _ProviderRouteModel.credential_account_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [_route_from_row(row) for row in rows]

    async def list_for_location(self, provider_key: str, location_id: str) -> list[ProviderRoute]:
        """Every account's observation of ONE location, selection order first."""
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        select(_ProviderRouteModel)
                        .where(
                            _ProviderRouteModel.provider_key == provider_key,
                            _ProviderRouteModel.location_id == location_id,
                        )
                        .order_by(
                            _ProviderRouteModel.priority,
                            _ProviderRouteModel.credential_account_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return [_route_from_row(row) for row in rows]

    async def account_ids_for_provider(self, provider_key: str) -> tuple[str, ...]:
        """Distinct credential accounts this provider has ever observed."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(_ProviderRouteModel.credential_account_id)
                    .where(_ProviderRouteModel.provider_key == provider_key)
                    .distinct()
                )
            ).all()
        return tuple(sorted(str(row[0]) for row in rows))

    async def count_for_account(self, provider_key: str, credential_account_id: str) -> int:
        """How many locations one account has observations for."""
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(_ProviderRouteModel.id).where(
                        _ProviderRouteModel.provider_key == provider_key,
                        _ProviderRouteModel.credential_account_id == credential_account_id,
                    )
                )
            ).all()
        return len(rows)
