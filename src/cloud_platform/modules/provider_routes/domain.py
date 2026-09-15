"""Provider-neutral credential-account routing knowledge (LEASEWEB-MULTIACCOUNT).

When one logical provider is served by several credential accounts, the
platform must durably remember **which account can actually serve which
location** — otherwise a checkout cannot know whose API key to bill, and a
recovery scan cannot know whose account-order inventory to search.

This module is deliberately provider-neutral: it speaks of a ``provider_key``,
an opaque ``credential_account_id`` and a ``location_id``. Nothing here knows
that Leaseweb API keys exist.

Availability discipline (the same one the catalog sync follows):

- a **definitive** provider answer (a 200 catalog listing, an empty listing, an
  account-level denial) updates the observed route;
- a **transient** failure (timeout, 429, 5xx) records the observation but must
  not erase last-known-good sellability;
- an **authentication** failure is per-account: it marks THAT account
  unhealthy and leaves every other account's routes untouched.

Route selection is deterministic — ``(priority, credential_account_id)`` — so
the same configuration always resolves the same fulfillment account. It is
never random dictionary order and never "cheapest provider cost" (that would
silently reprice customer offers).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from cloud_platform.providers.routing import (
    CredentialAccountState,
    normalize_account_id,
)

__all__ = [
    "DEFINITIVE_NEGATIVE_STATES",
    "ProviderRoute",
    "RouteObservation",
    "RouteState",
    "select_route_account",
    "serving_accounts",
]


class RouteState(StrEnum):
    """Observed eligibility of ONE (provider, account, location) triple."""

    ELIGIBLE_AVAILABLE = "eligible_available"
    """The account served this location and reported products."""

    ELIGIBLE_EMPTY = "eligible_empty"
    """The account served this location but reported no products."""

    INELIGIBLE = "ineligible"
    """The location is outside this account's (Sales Organization) scope."""

    TRANSIENT_UNKNOWN = "transient_unknown"
    """A transient failure: keep last-known availability, retry later."""

    AUTH_FAILED = "auth_failed"
    """This account's credential was rejected (401) — per-account problem."""

    DISABLED = "disabled"
    """The operator drained/disabled this account."""


#: States that are a DEFINITIVE negative answer for the location. Only these
#: may hide an offer; anything else preserves last-known-good sellability.
DEFINITIVE_NEGATIVE_STATES: frozenset[RouteState] = frozenset(
    {RouteState.ELIGIBLE_EMPTY, RouteState.INELIGIBLE, RouteState.DISABLED}
)


@dataclass(slots=True)
class ProviderRoute:
    """Durable observation: which account can serve which location."""

    provider_key: str
    credential_account_id: str
    location_id: str
    state: RouteState = RouteState.TRANSIENT_UNKNOWN
    priority: int = 100
    #: Product ids this account currently reports as available at the
    #: location. Empty means "no product-level evidence" (treated as
    #: unconstrained so a location-level route still routes).
    product_ids: tuple[str, ...] = ()
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    #: Exception CLASS name only — never a provider message, which can carry
    #: request context. Safe for logs and diagnostics.
    last_error_class: str | None = None
    #: Operator-owned account lifecycle mirrored from configuration, so route
    #: selection can honour draining/disabled without consulting config.
    account_state: CredentialAccountState = CredentialAccountState.ACTIVE

    def __post_init__(self) -> None:
        self.credential_account_id = normalize_account_id(self.credential_account_id)
        if not self.provider_key or not self.provider_key.strip():
            raise ValueError("provider_key must not be empty")
        if not self.location_id or not self.location_id.strip():
            raise ValueError("location_id must not be empty")
        if self.priority < 0:
            raise ValueError("priority must be >= 0")
        self.product_ids = tuple(self.product_ids)

    @property
    def key(self) -> tuple[str, str, str]:
        """The identity of one route row."""
        return (self.provider_key, self.credential_account_id, self.location_id)

    @property
    def is_serving(self) -> bool:
        """Whether this account can currently fulfil products here."""
        return self.state is RouteState.ELIGIBLE_AVAILABLE and self.accepts_new_orders

    @property
    def is_definitive_negative(self) -> bool:
        """Whether this route is a definitive "not available here" answer."""
        return self.state in DEFINITIVE_NEGATIVE_STATES

    @property
    def accepts_new_orders(self) -> bool:
        """Whether this account may receive NEW billable business."""
        return self.account_state is CredentialAccountState.ACTIVE

    def serves_product(self, product_id: str) -> bool:
        """Whether this account reports ``product_id`` available here.

        An empty ``product_ids`` means the observation is location-level only
        (no product-level evidence yet), so the route is treated as
        unconstrained rather than as "serves nothing".
        """
        return not self.product_ids or product_id in self.product_ids


@dataclass(frozen=True, slots=True)
class RouteObservation:
    """One account's fresh observation of one location (sync output)."""

    credential_account_id: str
    location_id: str
    state: RouteState
    product_ids: tuple[str, ...] = ()
    error_class: str | None = None
    succeeded: bool = False


def serving_accounts(
    routes: Iterable[ProviderRoute],
    *,
    location_id: str,
    product_id: str | None = None,
) -> tuple[ProviderRoute, ...]:
    """Every route that can serve ``location_id`` (optionally a product).

    Ordered deterministically by ``(priority, credential_account_id)`` so the
    first element is always the account a checkout must pin.
    """
    candidates = [
        route
        for route in routes
        if route.location_id == location_id
        and route.is_serving
        and (product_id is None or route.serves_product(product_id))
    ]
    return tuple(sorted(candidates, key=lambda r: (r.priority, r.credential_account_id)))


def select_route_account(
    routes: Iterable[ProviderRoute],
    *,
    location_id: str,
    product_id: str | None = None,
) -> str | None:
    """The deterministic fulfillment account for a location (+product).

    Returns ``None`` when no route can serve it — the caller must then refuse
    the purchase rather than guess an account.
    """
    candidates = serving_accounts(routes, location_id=location_id, product_id=product_id)
    return candidates[0].credential_account_id if candidates else None
