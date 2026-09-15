"""FX resolver: the single reusable path for currency conversion.

Routing (owns every formula; call sites never do ``price * rate``):

- identity (IRT->IRT, EUR->EUR, USD->USD, IRR->IRR): no provider call.
- exact ``1 IRT = 10 IRR`` both directions: no provider call.
- ``EUR <-> IRT`` through ``EURIRT``.
- ``USD(display) <-> IRT`` through ``USDTIRT`` as an EXPLICIT proxy
  (``proxy=true, proxy_asset="USDT"``); settlement via the proxy requires
  the operator flag.
- ``EUR <-> USD`` through IRT as the anchor
  (``EUR * EURIRT.buy / USDTIRT.buy`` for DISPLAY/CHARGE).
- ``EUR/USD <-> IRR`` via IRT anchor + exact x10.

Purpose selects the rate side: DISPLAY and CHARGE use the acquisition
(buy) side so the shown/charged price never undercharges; LIQUIDATION uses
the sell side. CHARGE/LIQUIDATION (financially binding) reject stale quotes
beyond ``charge_max_stale_seconds``; DISPLAY may use a bounded stale quote
up to ``max_stale_seconds``. Anything older fails closed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cloud_platform.modules.fx.domain import (
    ConversionSnapshot,
    FxError,
    FxInvalidQuoteError,
    FxMarketQuote,
    FxProxyNotAllowedError,
    FxPurpose,
    FxUnavailableError,
    FxUnsupportedCurrencyError,
    ResolvedMoney,
    major_to_minor,
    minor_to_major,
    normalize_currency,
)
from cloud_platform.modules.fx.ports import FxCache, FxRateSource, fx_cache_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FxConfig:
    """Operator-level FX policy (from ``[fx]`` + ``[fx.abantether]``)."""

    enabled: bool = True
    provider: str = "abantether"
    default_display_currency: str = "IRT"
    quote_ttl_seconds: int = 60
    max_stale_seconds: int = 300
    charge_max_stale_seconds: int = 30
    request_timeout_seconds: int = 5
    allow_usdt_proxy_for_display: bool = True
    allow_usdt_proxy_for_settlement: bool = False

    def __post_init__(self) -> None:
        if self.quote_ttl_seconds <= 0:
            raise ValueError("quote_ttl_seconds must be > 0")
        if self.max_stale_seconds < self.quote_ttl_seconds:
            raise ValueError("max_stale_seconds must be >= quote_ttl_seconds")
        if self.charge_max_stale_seconds < 0:
            raise ValueError("charge_max_stale_seconds must be >= 0")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be > 0")


@dataclass
class FxResolver:
    """Converts money using a rate source + last-known-good cache."""

    source: FxRateSource
    cache: FxCache
    config: FxConfig = field(default_factory=FxConfig)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def close(self) -> None:
        """Release source and cache transports (process owner calls once)."""
        for closable in (self.source, self.cache):
            closer = getattr(closable, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    logger.warning("fx close failed", exc_info=True)

    # -- public API --------------------------------------------------------

    async def resolve(
        self,
        amount_minor: int,
        source_currency: str,
        target_currency: str,
        purpose: FxPurpose,
    ) -> ResolvedMoney:
        """Convert ``amount_minor`` from source to target for ``purpose``."""
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise ValueError("amount_minor must be an integer (never float)")
        if amount_minor < 0:
            raise ValueError("amount_minor must be >= 0")
        src = normalize_currency(source_currency)
        dst = normalize_currency(target_currency)
        if not isinstance(purpose, FxPurpose):
            purpose = FxPurpose(str(purpose))
        if not self.config.enabled:
            raise FxUnavailableError("FX resolution is disabled by configuration")

        if src == dst:
            return self._identity(amount_minor, src, purpose)
        if {src, dst} == {"IRT", "IRR"}:
            return self._exact_irt_irr(amount_minor, src, dst, purpose)

        # Route through IRT anchor (with exact IRR legs when needed).
        if src == "IRR" or dst == "IRR":
            return await self._via_irt_anchor(amount_minor, src, dst, purpose)
        return await self._convert(amount_minor, src, dst, purpose)

    async def can_convert(
        self, source_currency: str, target_currency: str, purpose: FxPurpose
    ) -> bool:
        """Non-raising compatibility probe (gateway selection, UI gating)."""
        try:
            src = normalize_currency(source_currency)
            dst = normalize_currency(target_currency)
        except FxError:
            return False
        if src == dst or {src, dst} == {"IRT", "IRR"}:
            return True
        if src not in ("IRT", "IRR", "EUR", "USD") or dst not in ("IRT", "IRR", "EUR", "USD"):
            return False
        if self._needs_usdt_proxy(src, dst) and not self._proxy_allowed(purpose):
            return False
        if not self.config.enabled:
            return False
        return True

    async def snapshot(
        self,
        amount_minor: int,
        source_currency: str,
        target_currency: str,
        purpose: FxPurpose,
    ) -> ConversionSnapshot:
        """Resolve and freeze the conversion for durable persistence."""
        return ConversionSnapshot.from_resolved(
            await self.resolve(amount_minor, source_currency, target_currency, purpose)
        )

    # -- routing ------------------------------------------------------------

    @staticmethod
    def _needs_usdt_proxy(src: str, dst: str) -> bool:
        return src == "USD" or dst == "USD"

    def _proxy_allowed(self, purpose: FxPurpose) -> bool:
        if purpose is FxPurpose.DISPLAY:
            return self.config.allow_usdt_proxy_for_display
        return self.config.allow_usdt_proxy_for_settlement

    def _require_proxy(self, src: str, dst: str, purpose: FxPurpose) -> None:
        if self._needs_usdt_proxy(src, dst) and not self._proxy_allowed(purpose):
            raise FxProxyNotAllowedError(
                "USD conversion requires the USDT proxy, which is disabled "
                f"for purpose {purpose.value}"
            )

    def _identity(self, amount: int, currency: str, purpose: FxPurpose) -> ResolvedMoney:
        now = datetime.now(UTC)
        return ResolvedMoney(
            source_amount_minor=amount,
            source_currency=currency,
            target_amount_minor=amount,
            target_currency=currency,
            rate=Decimal(1),
            purpose=purpose,
            source="identity",
            path="identity",
            observed_at=now,
            expires_at=now + timedelta(seconds=self.config.quote_ttl_seconds),
            stale=False,
            proxy=False,
            proxy_asset="",
        )

    def _exact_irt_irr(self, amount: int, src: str, dst: str, purpose: FxPurpose) -> ResolvedMoney:
        now = datetime.now(UTC)
        if src == "IRT" and dst == "IRR":
            target = amount * 10
            rate = Decimal(10)
        else:
            # IRR -> IRT: exact /10 with purpose-directed rounding for the
            # sub-Toman remainder (CHARGE rounds up, never undercharges).
            target = major_to_minor(minor_to_major(amount, "IRR") / Decimal(10), "IRT", purpose)
            rate = Decimal("0.1")
        resolved = ResolvedMoney(
            source_amount_minor=amount,
            source_currency=src,
            target_amount_minor=target,
            target_currency=dst,
            rate=rate,
            purpose=purpose,
            source="exact",
            path="exact IRT<->IRR x10",
            observed_at=now,
            expires_at=now + timedelta(seconds=self.config.quote_ttl_seconds),
            stale=False,
            proxy=False,
            proxy_asset="",
        )
        self._observe(resolved, age_seconds=0)
        return resolved

    async def _via_irt_anchor(
        self, amount: int, src: str, dst: str, purpose: FxPurpose
    ) -> ResolvedMoney:
        """Handle legs involving IRR by converting the non-IRR side to IRT."""
        if src == "IRR":
            # IRR -> IRT (exact), then IRT -> dst (FX or identity).
            irt = major_to_minor(minor_to_major(amount, "IRR") / Decimal(10), "IRT", purpose)
            if dst == "IRT":
                return self._exact_irt_irr(amount, src, dst, purpose)
            second = await self._convert(irt, "IRT", dst, purpose)
            return ResolvedMoney(
                source_amount_minor=amount,
                source_currency=src,
                target_amount_minor=second.target_amount_minor,
                target_currency=dst,
                rate=second.rate / Decimal(10),
                purpose=purpose,
                source=second.source,
                path=f"exact IRR->IRT x10 + {second.path}",
                observed_at=second.observed_at,
                expires_at=second.expires_at,
                stale=second.stale,
                proxy=second.proxy,
                proxy_asset=second.proxy_asset,
            )
        # src -> IRT (FX), then IRT -> IRR (exact x10).
        first = await self._convert(amount, src, "IRT", purpose)
        final_amount = first.target_amount_minor * 10
        return ResolvedMoney(
            source_amount_minor=amount,
            source_currency=src,
            target_amount_minor=final_amount,
            target_currency=dst,
            rate=first.rate * Decimal(10),
            purpose=purpose,
            source=first.source,
            path=f"{first.path} + exact IRT->IRR x10",
            observed_at=first.observed_at,
            expires_at=first.expires_at,
            stale=first.stale,
            proxy=first.proxy,
            proxy_asset=first.proxy_asset,
        )

    async def _convert(self, amount: int, src: str, dst: str, purpose: FxPurpose) -> ResolvedMoney:
        if src not in ("IRT", "EUR", "USD") or dst not in ("IRT", "EUR", "USD"):
            raise FxUnsupportedCurrencyError(f"no FX route for {src}->{dst}")
        self._require_proxy(src, dst, purpose)
        use_buy = purpose is not FxPurpose.LIQUIDATION

        if {src, dst} <= {"EUR", "IRT"}:
            quote = await self._quote("EUR", "IRT", purpose)
            return self._apply_single(amount, src, dst, quote, purpose, use_buy)
        if {src, dst} <= {"USD", "IRT"}:
            quote = await self._quote("USDT", "IRT", purpose)
            return self._apply_single_proxy(amount, src, dst, quote, purpose, use_buy)
        # EUR <-> USD through IRT.
        eur_irt = await self._quote("EUR", "IRT", purpose)
        usdt_irt = await self._quote("USDT", "IRT", purpose)
        return self._apply_cross(amount, src, dst, eur_irt, usdt_irt, purpose, use_buy)

    def _apply_single(
        self,
        amount: int,
        src: str,
        dst: str,
        quote: FxMarketQuote,
        purpose: FxPurpose,
        use_buy: bool,
    ) -> ResolvedMoney:
        rate = quote.buy_rate if use_buy else quote.sell_rate
        side = "buy" if use_buy else "sell"
        source_major = minor_to_major(amount, src)
        if src == "EUR" and dst == "IRT":
            target = major_to_minor(source_major * rate, "IRT", purpose)
        elif src == "IRT" and dst == "EUR":
            target = major_to_minor(source_major / rate, "EUR", purpose)
        else:  # pragma: no cover - routing guarantees the two cases above
            raise FxUnsupportedCurrencyError(f"no FX route for {src}->{dst}")
        resolved = ResolvedMoney(
            source_amount_minor=amount,
            source_currency=src,
            target_amount_minor=target,
            target_currency=dst,
            rate=rate,
            purpose=purpose,
            source=quote.source,
            path=f"{quote.source_market}.{side}",
            observed_at=quote.observed_at,
            expires_at=quote.expires_at,
            stale=self._is_stale(quote),
            proxy=False,
            proxy_asset="",
        )
        self._observe(resolved, age_seconds=self._age(quote))
        return resolved

    def _apply_single_proxy(
        self,
        amount: int,
        src: str,
        dst: str,
        quote: FxMarketQuote,
        purpose: FxPurpose,
        use_buy: bool,
    ) -> ResolvedMoney:
        rate = quote.buy_rate if use_buy else quote.sell_rate
        side = "buy" if use_buy else "sell"
        source_major = minor_to_major(amount, src)
        if src == "USD" and dst == "IRT":
            target = major_to_minor(source_major * rate, "IRT", purpose)
        elif src == "IRT" and dst == "USD":
            target = major_to_minor(source_major / rate, "USD", purpose)
        else:  # pragma: no cover - routing guarantees the two cases above
            raise FxUnsupportedCurrencyError(f"no FX route for {src}->{dst}")
        resolved = ResolvedMoney(
            source_amount_minor=amount,
            source_currency=src,
            target_amount_minor=target,
            target_currency=dst,
            rate=rate,
            purpose=purpose,
            source=quote.source,
            path=f"{quote.source_market}.{side} (proxy USDT for USD)",
            observed_at=quote.observed_at,
            expires_at=quote.expires_at,
            stale=self._is_stale(quote),
            proxy=True,
            proxy_asset="USDT",
        )
        self._observe(resolved, age_seconds=self._age(quote))
        return resolved

    def _apply_cross(
        self,
        amount: int,
        src: str,
        dst: str,
        eur_irt: FxMarketQuote,
        usdt_irt: FxMarketQuote,
        purpose: FxPurpose,
        use_buy: bool,
    ) -> ResolvedMoney:
        eur_rate = eur_irt.buy_rate if use_buy else eur_irt.sell_rate
        usdt_rate = usdt_irt.buy_rate if use_buy else usdt_irt.sell_rate
        side = "buy" if use_buy else "sell"
        source_major = minor_to_major(amount, src)
        if src == "EUR" and dst == "USD":
            target = major_to_minor(source_major * eur_rate / usdt_rate, "USD", purpose)
            rate = eur_rate / usdt_rate
        elif src == "USD" and dst == "EUR":
            target = major_to_minor(source_major * usdt_rate / eur_rate, "EUR", purpose)
            rate = usdt_rate / eur_rate
        else:  # pragma: no cover - routing guarantees EUR<->USD
            raise FxUnsupportedCurrencyError(f"no FX route for {src}->{dst}")
        observed = min(eur_irt.observed_at, usdt_irt.observed_at)
        expires = min(eur_irt.expires_at, usdt_irt.expires_at)
        stale = self._is_stale(eur_irt) or self._is_stale(usdt_irt)
        resolved = ResolvedMoney(
            source_amount_minor=amount,
            source_currency=src,
            target_amount_minor=target,
            target_currency=dst,
            rate=rate,
            purpose=purpose,
            source=eur_irt.source,
            path=f"EURIRT.{side}/USDTIRT.{side} via IRT (proxy USDT for USD)",
            observed_at=observed,
            expires_at=expires,
            stale=stale,
            proxy=True,
            proxy_asset="USDT",
        )
        self._observe(resolved, age_seconds=max(self._age(eur_irt), self._age(usdt_irt)))
        return resolved

    # -- quote fetching with bounded staleness --------------------------------

    async def _quote(self, base: str, quote: str, purpose: FxPurpose) -> FxMarketQuote:
        key = fx_cache_key(base, quote)
        cached = await self._safe_cache_get(key)
        now = datetime.now(UTC)
        if cached is not None and now <= cached.expires_at:
            self._metric_cache_hit()
            return cached
        # Expired or absent: fetch live (singleflight per market).
        async with self._lock_for(key):
            # Re-check inside the lock: a concurrent fetch may have refreshed.
            recached = await self._safe_cache_get(key)
            if recached is not None and datetime.now(UTC) <= recached.expires_at:
                self._metric_cache_hit()
                return recached
            try:
                fresh = await self.source.get_quote(base, quote)
            except Exception as exc:
                self._metric_request(base, quote, purpose, "error", False, False)
                return self._fallback_or_raise(key, cached, purpose, exc)
            self._validate_quote(fresh, base, quote)
            await self._safe_cache_put(key, fresh)
            self._metric_request(base, quote, purpose, "ok", False, False)
            return fresh

    def _validate_quote(self, quote: FxMarketQuote, base: str, market_quote: str) -> None:
        if not isinstance(quote, FxMarketQuote):
            raise FxInvalidQuoteError("rate source returned a non-quote")
        if quote.base_currency != base or quote.quote_currency != market_quote:
            raise FxInvalidQuoteError(
                f"rate source returned {quote.base_currency}->{quote.quote_currency}, "
                f"expected {base}->{market_quote}"
            )

    def _fallback_or_raise(
        self, key: str, cached: FxMarketQuote | None, purpose: FxPurpose, exc: Exception
    ) -> FxMarketQuote:
        limit = self._stale_limit(purpose)
        if cached is not None and self._age(cached) <= limit:
            logger.warning(
                "fx live fetch failed; using bounded stale quote",
                extra={
                    "source": getattr(self.source, "source_name", "unknown"),
                    "market": key,
                    "purpose": purpose.value,
                    "stale": True,
                    "age_seconds": self._age(cached),
                },
            )
            self._metric_stale_use()
            return cached
        if isinstance(exc, FxError):
            raise exc
        raise FxUnavailableError(f"FX market {key} is unavailable") from exc

    def _stale_limit(self, purpose: FxPurpose) -> int:
        if purpose is FxPurpose.DISPLAY:
            return self.config.max_stale_seconds
        return self.config.charge_max_stale_seconds

    @staticmethod
    def _age(quote: FxMarketQuote) -> float:
        return max(0.0, (datetime.now(UTC) - quote.observed_at).total_seconds())

    def _is_stale(self, quote: FxMarketQuote) -> bool:
        return datetime.now(UTC) > quote.expires_at

    async def _safe_cache_get(self, key: str) -> FxMarketQuote | None:
        try:
            return await self.cache.get(key)
        except Exception as exc:
            logger.warning("fx cache get failed: %s", type(exc).__name__)
            return None

    async def _safe_cache_put(self, key: str, quote: FxMarketQuote) -> None:
        try:
            await self.cache.put(key, quote)
        except Exception as exc:
            logger.warning("fx cache put failed: %s", type(exc).__name__)

    # -- observability (non-secret, closed label sets) -------------------------

    def _observe(self, resolved: ResolvedMoney, *, age_seconds: float) -> None:
        logger.info(
            "fx resolved %s->%s purpose=%s market=%s stale=%s proxy=%s age=%.1fs",
            resolved.source_currency,
            resolved.target_currency,
            resolved.purpose.value,
            resolved.path,
            resolved.stale,
            resolved.proxy,
            age_seconds,
            extra={
                "source": resolved.source,
                "base": resolved.source_currency,
                "quote": resolved.target_currency,
                "purpose": resolved.purpose.value,
                "market": resolved.path,
                "stale": resolved.stale,
                "proxy": resolved.proxy,
                "age_seconds": round(age_seconds, 1),
            },
        )
        self._metric_request(
            resolved.source_currency,
            resolved.target_currency,
            resolved.purpose,
            "ok",
            resolved.stale,
            resolved.proxy,
        )

    def _metric_request(
        self,
        base: str,
        quote: str,
        purpose: FxPurpose,
        outcome: str,
        stale: bool,
        proxy: bool,
    ) -> None:
        try:
            from cloud_platform.observability.metrics import metrics

            counter = getattr(metrics, "fx_quote_requests_total", None)
            if counter is not None:
                counter.labels(
                    source=getattr(self.source, "source_name", "unknown"),
                    base=base,
                    quote=quote,
                    purpose=purpose.value if isinstance(purpose, FxPurpose) else str(purpose),
                    outcome=outcome,
                ).inc()
            if stale:
                stale_counter = getattr(metrics, "fx_stale_quote_uses_total", None)
                if stale_counter is not None:
                    stale_counter.inc()
            _ = proxy
        except Exception:
            pass

    def _metric_cache_hit(self) -> None:
        try:
            from cloud_platform.observability.metrics import metrics

            counter = getattr(metrics, "fx_cache_hits_total", None)
            if counter is not None:
                counter.inc()
        except Exception:
            pass

    def _metric_stale_use(self) -> None:
        try:
            from cloud_platform.observability.metrics import metrics

            counter = getattr(metrics, "fx_stale_quote_uses_total", None)
            if counter is not None:
                counter.inc()
        except Exception:
            pass


__all__ = ["FxConfig", "FxResolver"]
