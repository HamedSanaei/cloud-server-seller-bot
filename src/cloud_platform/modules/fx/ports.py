"""FX ports: rate-source and cache abstractions (provider-neutral).

The domain/application layers depend only on these protocols. AbanTether is
one ``FxRateSource`` implementation; the cache has an in-memory and a Redis
implementation. Neither protocol mentions a concrete provider, Telegram,
FastAPI, SQLAlchemy or httpx.
"""

from __future__ import annotations

from typing import Protocol

from cloud_platform.modules.fx.domain import FxMarketQuote


class FxRateSource(Protocol):
    """Read-only live rate source (one implementation per FX provider)."""

    source_name: str

    async def get_quote(self, base_currency: str, quote_currency: str) -> FxMarketQuote:
        """Fetch a fresh validated quote (raises :class:`FxError` when unusable)."""
        ...

    async def close(self) -> None:
        """Release the underlying transport (exactly once per process owner)."""
        ...


class FxCache(Protocol):
    """Durable last-known-good quote store (TTL + bounded staleness)."""

    async def get(self, key: str) -> FxMarketQuote | None:
        """Return the cached quote for ``key`` (fresh or stale), if any."""
        ...

    async def put(self, key: str, quote: FxMarketQuote) -> None:
        """Store ``quote`` under ``key`` (overwrites)."""
        ...

    async def close(self) -> None:
        """Release any underlying connection (no-op for in-memory)."""
        ...


def fx_cache_key(base_currency: str, quote_currency: str) -> str:
    """Stable cache key for one directed market (e.g. ``EUR->IRT``)."""
    return f"{base_currency.strip().upper()}->{quote_currency.strip().upper()}"


__all__ = ["FxCache", "FxRateSource", "fx_cache_key"]
