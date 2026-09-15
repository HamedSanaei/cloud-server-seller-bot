"""FX quote caches: in-memory (tests/dev) and Redis (production).

Both implement the :class:`FxCache` port. Entries carry the quote itself;
freshness is derived from ``observed_at``/``expires_at`` by the resolver, so
the cache never invents a TTL — it only stores and returns documents.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from cloud_platform.modules.fx.domain import FxMarketQuote

logger = logging.getLogger(__name__)


def quote_to_document(quote: FxMarketQuote) -> str:
    return json.dumps(
        {
            "base_currency": quote.base_currency,
            "quote_currency": quote.quote_currency,
            "buy_rate": str(quote.buy_rate),
            "sell_rate": str(quote.sell_rate),
            "source": quote.source,
            "source_market": quote.source_market,
            "observed_at": quote.observed_at.isoformat(),
            "expires_at": quote.expires_at.isoformat(),
            "proxy": quote.proxy,
            "proxy_asset": quote.proxy_asset,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def quote_from_document(raw: Any) -> FxMarketQuote | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("fx cache: malformed JSON payload rejected")
        return None
    if not isinstance(data, dict):
        return None
    try:
        return FxMarketQuote(
            base_currency=str(data["base_currency"]),
            quote_currency=str(data["quote_currency"]),
            buy_rate=Decimal(str(data["buy_rate"])),
            sell_rate=Decimal(str(data["sell_rate"])),
            source=str(data["source"]),
            source_market=str(data["source_market"]),
            observed_at=datetime.fromisoformat(str(data["observed_at"])),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
            proxy=bool(data.get("proxy", False)),
            proxy_asset=str(data.get("proxy_asset", "")),
        )
    except Exception:
        logger.warning("fx cache: unrecognised quote document rejected")
        return None


class InMemoryFxCache:
    """Single-process FX cache (tests/dev). No locking beyond one event loop."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def get(self, key: str) -> FxMarketQuote | None:
        return quote_from_document(self._values.get(key))

    async def put(self, key: str, quote: FxMarketQuote) -> None:
        self._values[key] = quote_to_document(quote)

    async def close(self) -> None:
        return None

    def clear(self) -> None:
        self._values.clear()


class RedisFxCache:
    """Shared FX last-known-good store (production).

    Keys are namespaced (``<prefix>:fx:v1:<market>``) so one Redis database
    can hold bot sessions and FX quotes without collisions. Values are the
    same JSON documents as the in-memory cache; a Redis outage surfaces as
    ``None`` (cache miss) so the resolver fails over to a live fetch — the
    caller still fails closed when both are unavailable.
    """

    def __init__(self, client: Any, *, prefix: str = "cloud-platform") -> None:
        self._client = client
        self._prefix = prefix.rstrip(":")

    def _key(self, market: str) -> str:
        safe = market.strip().upper().replace(" ", "")
        return f"{self._prefix}:fx:v1:{safe}"

    async def get(self, key: str) -> FxMarketQuote | None:
        try:
            raw = await self._client.get(self._key(key))
        except Exception as exc:
            logger.warning("fx cache get failed: %s", type(exc).__name__)
            return None
        return quote_from_document(raw)

    async def put(self, key: str, quote: FxMarketQuote) -> None:
        # Last-known-good must survive longer than one TTL: keep it for the
        # bounded stale window (24h cap) so a provider outage degrades to a
        # marked-stale quote instead of losing the market entirely.
        try:
            await self._client.set(self._key(key), quote_to_document(quote), ex=86_400)
        except Exception as exc:
            logger.warning("fx cache put failed: %s", type(exc).__name__)

    async def close(self) -> None:
        closer = getattr(self._client, "aclose", None)
        if callable(closer):
            try:
                await closer()
            except Exception:
                pass


def build_fx_cache(*, backend: str, redis_url: str = "", prefix: str = "cloud-platform") -> Any:
    """Build the FX cache for ``backend`` (``redis`` or ``memory``)."""
    normalised = (backend or "memory").strip().lower()
    if normalised == "redis":
        if not redis_url:
            raise ValueError("redis_url is required for the redis FX cache")
        from cloud_platform.core.redis import create_redis_client

        return RedisFxCache(create_redis_client(redis_url), prefix=prefix)
    if normalised == "memory":
        return InMemoryFxCache()
    raise ValueError(f"unknown FX cache backend {backend!r}")


__all__ = [
    "InMemoryFxCache",
    "RedisFxCache",
    "build_fx_cache",
    "quote_from_document",
    "quote_to_document",
]
