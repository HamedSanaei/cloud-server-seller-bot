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

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from cloud_platform.modules.provider_routes.domain import (
    ProviderRoute,
    select_route_account,
)

__all__ = ["ProviderRouteSelector"]


class ProviderRouteSelector:
    """Resolves the fulfillment credential account for a location/product."""

    def __init__(
        self,
        session_factory: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
        *,
        repository: Any | None = None,
    ) -> None:
        if repository is None:
            from cloud_platform.modules.provider_routes.repository import (
                SqlAlchemyProviderRouteRepository,
            )

            if session_factory is None:  # pragma: no cover - construction guard
                raise ValueError("a session_factory or repository is required")
            repository = SqlAlchemyProviderRouteRepository(session_factory)
        self._repository = repository

    async def routes_for(self, provider_key: str, location_id: str) -> list[ProviderRoute]:
        """Every account's observation of one location (selection order)."""
        return await self._repository.list_for_location(provider_key, location_id)

    async def account_for(
        self,
        provider_key: str,
        location_id: str,
        product_id: str | None = None,
    ) -> str | None:
        """The credential account a purchase here must be PINNED to.

        Returns ``None`` when the provider has no credential accounts at all
        (legacy single-credential deployment). Raises nothing itself: callers
        decide the refusal, so the failure carries their own domain error.
        """
        routes = await self.routes_for(provider_key, location_id)
        if not routes:
            return None
        return select_route_account(routes, location_id=location_id, product_id=product_id)

    async def has_any_routes(self, provider_key: str) -> bool:
        """Whether the provider has any routing knowledge persisted yet."""
        return bool(await self._repository.account_ids_for_provider(provider_key))
