"""Provider-neutral credential-account routing knowledge.

One logical provider key can be served by many credential accounts. This module
durably records which account can serve which location and resolves the
deterministic fulfillment account for a purchase — without any module outside
the provider's own adapter knowing what a credential looks like.
"""

from cloud_platform.modules.provider_routes.domain import (
    DEFINITIVE_NEGATIVE_STATES,
    ProviderRoute,
    RouteObservation,
    RouteState,
    select_route_account,
    serving_accounts,
)
from cloud_platform.modules.provider_routes.repository import (
    SqlAlchemyProviderRouteRepository,
)

__all__ = [
    "DEFINITIVE_NEGATIVE_STATES",
    "ProviderRoute",
    "RouteObservation",
    "RouteState",
    "SqlAlchemyProviderRouteRepository",
    "select_route_account",
    "serving_accounts",
]
