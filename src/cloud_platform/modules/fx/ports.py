"""FX ports: rate-source and cache abstractions (provider-neutral).

The domain/application layers depend only on these protocols. AbanTether is
one ``FxRateSource`` implementation; the cache has an in-memory and a Redis
implementation. Neither protocol mentions a concrete provider, Telegram,
FastAPI, SQLAlchemy or httpx.
"""

from __future__ import annotations

from typing import Protocol

from cloud_platform.modules.fx.domain import FxMarketQuote, FxReferenceQuote


class FxRateSource(Protocol):
    """Read-only live rate source (one implementation per FX provider)."""

    source_name: str

    async def get_quote(self, base_currency: str, quote_currency: str) -> FxMarketQuote:
        """Fetch a fresh validated quote (raises :class:`FxError` when unusable)."""
        ...

    async def close(self) -> None:
        """Release the underlying transport (exactly once per process owner)."""
        ...


class FxReferenceRateSource(Protocol):
    """Official reference-rate source with no bid/ask semantics."""

    source_name: str

    async def get_reference_rate(self, base_currency: str, quote_currency: str) -> FxReferenceQuote:
        """Fetch one validated positive finite reference rate."""
        ...

    async def close(self) -> None:
        """Release the underlying transport (exactly once per process owner)."""
        ...


FxQuote = FxMarketQuote | FxReferenceQuote


class FxCache(Protocol):
    """Durable last-known-good quote store (TTL + bounded staleness)."""

    async def get(self, key: str) -> FxQuote | None:
        """Return the cached quote for ``key`` (fresh or stale), if any."""
        ...

    async def put(self, key: str, quote: FxQuote) -> None:
        """Store ``quote`` under ``key`` (overwrites)."""
        ...

    async def close(self) -> None:
        """Release any underlying connection (no-op for in-memory)."""
        ...


def fx_cache_key(base_currency: str, quote_currency: str, *, source: str = "domestic") -> str:
    """Stable versioned key including the source family."""
    family = (source or "domestic").strip().lower().replace(" ", "-")
    pair = f"{base_currency.strip().upper()}->{quote_currency.strip().upper()}"
    return f"market:v2:{family}:{pair}"


def reference_rate_cache_key(
    base_currency: str, quote_currency: str, *, source: str = "global"
) -> str:
    """Stable namespaced key including the global source family."""
    family = (source or "global").strip().lower().replace(" ", "-")
    pair = f"{base_currency.strip().upper()}->{quote_currency.strip().upper()}"
    return f"reference:v2:{family}:{pair}"


__all__ = [
    "FxCache",
    "FxQuote",
    "FxRateSource",
    "FxReferenceRateSource",
    "fx_cache_key",
    "reference_rate_cache_key",
]
