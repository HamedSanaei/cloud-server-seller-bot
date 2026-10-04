"""Deterministic fulfillment-account selection (LEASEWEB-MULTIACCOUNT).

The checkout must pin WHICH provider credential account will place the billable
order before anything is persisted, so the account is a durable fact rather
than something re-derived (and possibly re-decided) later. This service answers
exactly one question, provider-neutrally:

    "which credential account serves this location (and product)?"

Selection policy, in order:

1. the route must be currently ELIGIBLE and AVAILABLE;
2. the account must accept new orders (not draining/disabled/disabled-by-config);
3. the account must report the product (a location-level-only observation is
   treated as unconstrained rather than as "serves nothing");
4. ties break deterministically by ``(priority, credential_account_id)``.

There is deliberately no "cheapest provider cost" rule: routing on provider
cost would silently reprice customer offers, which is an explicit product
decision, not an implementation detail.

``None`` means "no account known", and is only valid for providers that have no
credential accounts at all. When a provider HAS routes and none serves the
request, the caller must refuse the purchase — never guess an account.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any

from cloud_platform.modules.provider_routes.domain import (
    ProviderRoute,
    RouteState,
    select_route_account,
    serving_accounts,
)
from cloud_platform.providers.base import AccountServerUsageReader
from cloud_platform.providers.errors import (
    ProviderCapacityError,
    ProviderNotFound,
    ProviderUnavailable,
)

__all__ = ["ProviderRouteSelector"]


class ProviderRouteSelector:
    """Resolves the fulfillment credential account for a location/product."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        *,
        repository: Any | None = None,
        usage_readers: Mapping[str, AccountServerUsageReader] | None = None,
    ) -> None:
        if repository is None:
            from cloud_platform.modules.provider_routes.repository import (
                SqlAlchemyProviderRouteRepository,
            )

            if session_factory is None:  # pragma: no cover - construction guard
                raise ValueError("a session_factory or repository is required")
            repository = SqlAlchemyProviderRouteRepository(session_factory)
        self._repository = repository
        self._usage_readers = dict(usage_readers or {})

    def supports_capacity_failover(self, provider_key: str) -> bool:
        return provider_key in self._usage_readers

    async def routes_for(self, provider_key: str, location_id: str) -> list[ProviderRoute]:
        """Every account's observation of one location (selection order)."""
        return await self._repository.list_for_location(provider_key, location_id)

    async def account_for(
        self,
        provider_key: str,
        location_id: str,
        product_id: str | None = None,
        *,
        exclude: Collection[str] = (),
    ) -> str | None:
        """Select only independently proven routes with readable live headroom."""
        routes = await self.routes_for(provider_key, location_id)
        reader = self._usage_readers.get(provider_key)
        if reader is None:
            return select_route_account(
                (route for route in routes if route.credential_account_id not in exclude),
                location_id=location_id,
                product_id=product_id,
            )
        candidates = [
            route
            for route in serving_accounts(routes, location_id=location_id, product_id=product_id)
            if reader.accepts_new_orders(route.credential_account_id)
            and (product_id is None or product_id in route.product_ids)
        ]
        unreadable = any(
            route.state in (RouteState.TRANSIENT_UNKNOWN, RouteState.AUTH_FAILED)
            and route.credential_account_id not in exclude
            and reader.accepts_new_orders(route.credential_account_id)
            and (not route.product_ids or product_id is None or product_id in route.product_ids)
            for route in routes
        )
        if not candidates:
            if unreadable:
                raise ProviderUnavailable("current serving eligibility could not be proved")
            raise ProviderNotFound("no proven serving product/location route")
        for route in candidates:
            account_id = route.credential_account_id
            if account_id in exclude:
                continue
            try:
                usage = await reader.server_usage(account_id)
                if usage.credential_account_id != account_id:
                    raise ProviderUnavailable("inventory credential identity mismatch")
            except Exception:
                unreadable = True
                continue
            if not usage.full:
                return account_id
        if unreadable:
            raise ProviderUnavailable("current Project capacity could not be proved")
        raise ProviderCapacityError("all proven eligible provider accounts are full or refused")

    async def has_any_routes(self, provider_key: str) -> bool:
        """Whether the provider has any routing knowledge persisted yet."""
        return bool(await self._repository.account_ids_for_provider(provider_key))
