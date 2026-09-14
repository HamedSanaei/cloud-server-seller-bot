"""Storefront markets and the provider listing (Iran / Foreign).

The customer picks a **market** before seeing providers, locations or plans::

    خرید سرور -> 🇮🇷 سرور ایران | 🌍 سرور خارج -> provider -> location -> ...

A market is a *customer-facing grouping*, not a provider property: the
platform must be able to add another Iranian or foreign provider without
touching this module or any domain service. Everything here is therefore
driven by operator configuration (``[providers.<key>] market`` /
``display_name`` / ``enabled`` in ``configuration.toml``):

- :class:`Market` — the two sellable markets (``iran`` / ``foreign``);
- :class:`ProviderCatalog` — the configured provider metadata;
- :class:`ProviderListing` — one provider as the storefront shows it.

Ordering capability is deliberately NOT modelled here: whether a provider can
actually take an order for a monthly offer is a *capability* question that the
application layer answers through the provider port
(:func:`cloud_platform.providers.base.ordering_support_of`). Domain and
application code never branch on a concrete provider name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class MarketError(Exception):
    """Base error for market/provider-listing operations."""


class UnknownMarketError(MarketError):
    """The market value is not one the storefront knows."""


class Market(StrEnum):
    """The customer-facing markets of the storefront."""

    IRAN = "iran"
    FOREIGN = "foreign"

    @property
    def label_key(self) -> str:
        """Message-catalog key of the market button."""
        return f"store.market_{self.value}"

    @property
    def title_key(self) -> str:
        """Message-catalog key of the market screen title."""
        return f"store.market_title_{self.value}"


#: Render order of the markets (Persian-first UI: Iran first).
MARKET_ORDER: tuple[Market, ...] = (Market.IRAN, Market.FOREIGN)


def parse_market(value: str) -> Market:
    """Parse a configured market value (case-insensitive)."""
    try:
        return Market(str(value).strip().lower())
    except ValueError as exc:
        raise UnknownMarketError(f"unknown market {value!r}") from exc


@dataclass(frozen=True, slots=True)
class ProviderListing:
    """One provider as the storefront presents it."""

    provider_key: str
    market: Market
    display_name: str
    enabled: bool = True
    ordering_capable: bool = True

    @property
    def buyable(self) -> bool:
        """Whether the provider can take an order right now."""
        return self.enabled and self.ordering_capable


class ProviderCatalog:
    """Config-driven provider metadata (one instance per process)."""

    def __init__(
        self,
        *,
        markets: Mapping[str, str] | None = None,
        display_names: Mapping[str, str] | None = None,
        enabled: Mapping[str, bool] | None = None,
    ) -> None:
        self._markets = {str(k): str(v) for k, v in (markets or {}).items()}
        self._display_names = {str(k): str(v) for k, v in (display_names or {}).items()}
        self._enabled = {str(k): bool(v) for k, v in (enabled or {}).items()}

    # -- lookups -----------------------------------------------------------

    def market_of(self, provider_key: str) -> Market | None:
        """The configured market of a provider (None when unconfigured)."""
        raw = self._markets.get(provider_key)
        if not raw:
            return None
        try:
            return parse_market(raw)
        except UnknownMarketError:
            return None

    def display_name_of(self, provider_key: str) -> str:
        """The customer-facing provider name (never an internal key)."""
        return self._display_names.get(provider_key) or provider_key

    def is_enabled(self, provider_key: str) -> bool:
        """Operator switch: unset means enabled (credentials still decide)."""
        return self._enabled.get(provider_key, True)

    def listing(
        self,
        provider_key: str,
        *,
        ordering_capable: bool,
        market: Market | None = None,
    ) -> ProviderListing | None:
        """Build the listing for a provider, or None when it has no market."""
        resolved = market or self.market_of(provider_key)
        if resolved is None:
            return None
        return ProviderListing(
            provider_key=provider_key,
            market=resolved,
            display_name=self.display_name_of(provider_key),
            enabled=self.is_enabled(provider_key),
            ordering_capable=ordering_capable,
        )


__all__ = [
    "MARKET_ORDER",
    "Market",
    "MarketError",
    "ProviderCatalog",
    "ProviderListing",
    "UnknownMarketError",
    "parse_market",
]
