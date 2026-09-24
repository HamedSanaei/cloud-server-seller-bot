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
from decimal import Decimal, localcontext

from cloud_platform.modules.fx.domain import (
    FX_MATH_PRECISION,
    GLOBAL_FIAT_CURRENCIES,
    SUPPORTED_CURRENCIES,
    ConversionSnapshot,
    FxError,
    FxInvalidQuoteError,
    FxMarketQuote,
    FxProxyNotAllowedError,
    FxPurpose,
    FxReferenceQuote,
    FxStaleError,
    FxUnavailableError,
    FxUnsupportedCurrencyError,
    ResolvedMoney,
    major_to_minor,
    minor_to_major,
    normalize_currency,
)
from cloud_platform.modules.fx.ports import (
    FxCache,
    FxRateSource,
    FxReferenceRateSource,
    fx_cache_key,
    reference_rate_cache_key,
)

logger = logging.getLogger(__name__)


def _decimal_mul(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = FX_MATH_PRECISION
        return left * right


def _decimal_div(numerator: Decimal, denominator: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = FX_MATH_PRECISION
        return numerator / denominator


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
        for name, bool_value in (
            ("enabled", self.enabled),
            ("allow_usdt_proxy_for_display", self.allow_usdt_proxy_for_display),
            ("allow_usdt_proxy_for_settlement", self.allow_usdt_proxy_for_settlement),
        ):
            if not isinstance(bool_value, bool):
                raise ValueError(f"{name} must be boolean")
        for name, int_value in (
            ("quote_ttl_seconds", self.quote_ttl_seconds),
            ("max_stale_seconds", self.max_stale_seconds),
            ("charge_max_stale_seconds", self.charge_max_stale_seconds),
            ("request_timeout_seconds", self.request_timeout_seconds),
        ):
            if isinstance(int_value, bool) or not isinstance(int_value, int):
                raise ValueError(f"{name} must be an integer")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string")
        object.__setattr__(
            self, "default_display_currency", normalize_currency(self.default_display_currency)
        )
        if self.default_display_currency not in SUPPORTED_CURRENCIES:
            raise ValueError("default_display_currency must be an audited supported currency")
        if self.quote_ttl_seconds <= 0:
            raise ValueError("quote_ttl_seconds must be > 0")
        if self.max_stale_seconds < self.quote_ttl_seconds:
            raise ValueError("max_stale_seconds must be >= quote_ttl_seconds")
        if self.enabled and self.charge_max_stale_seconds <= 0:
            raise ValueError("charge_max_stale_seconds must be > 0")
        if self.charge_max_stale_seconds > self.max_stale_seconds:
            raise ValueError("charge_max_stale_seconds must be <= max_stale_seconds")
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
        if src not in SUPPORTED_CURRENCIES or dst not in SUPPORTED_CURRENCIES:
            raise FxUnsupportedCurrencyError(f"unsupported FX currency pair {src}->{dst}")
        if not self.config.enabled:
            raise FxUnavailableError("FX resolution is disabled by configuration")

        if src == dst:
            return self._identity(amount_minor, src, purpose)
        if {src, dst} == {"IRT", "IRR"}:
            return self._exact_irt_irr(amount_minor, src, dst, purpose)
        # Every USD leg, including IRR<->USD via the IRT anchor, must obey the
        # explicit USDT-proxy policy; otherwise resolve could bypass the
        # settlement guard that can_convert enforces.
        self._require_proxy(src, dst, purpose)

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
            purpose = FxPurpose(purpose if isinstance(purpose, FxPurpose) else str(purpose))
        except (FxError, ValueError):
            return False
        if src not in SUPPORTED_CURRENCIES or dst not in SUPPORTED_CURRENCIES:
            return False
        if src == dst:
            return True
        if src not in {"IRT", "IRR", "EUR", "USD"} or dst not in {
            "IRT",
            "IRR",
            "EUR",
            "USD",
        }:
            return False
        if src == dst or {src, dst} == {"IRT", "IRR"}:
            return True
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
        with localcontext() as context:
            context.prec = 50
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
            path=f"exact {src}<->{dst} x{rate}",
            observed_at=now,
            expires_at=now + timedelta(seconds=self.config.quote_ttl_seconds),
            stale=False,
            proxy=False,
            proxy_asset="",
        )
        self._observe(resolved, age_seconds=Decimal(0))
        return resolved

    async def _via_irt_anchor(
        self, amount: int, src: str, dst: str, purpose: FxPurpose
    ) -> ResolvedMoney:
        """Compose a domestic IRR/IRT leg with one final rounding boundary.

        The IRT anchor is a rate relationship, not a wallet/accounting leg.
        Rounding an intermediate IRT amount would change the amount sent to
        the final quote and can under- or over-charge by one IRT unit.  Keep
        the exact Decimal composition here and quantize only the final
        customer's currency.
        """
        if {src, dst} == {"IRT", "IRR"}:
            return self._exact_irt_irr(amount, src, dst, purpose)
        if src == "IRR":
            if dst == "EUR":
                quote = await self._quote("EUR", "IRT", purpose)
                side_rate = (
                    quote.buy_rate if purpose is not FxPurpose.LIQUIDATION else quote.sell_rate
                )
                side = "buy" if purpose is not FxPurpose.LIQUIDATION else "sell"
                rate = _decimal_div(Decimal(1), _decimal_mul(Decimal(10), side_rate))
                target = major_to_minor(
                    _decimal_mul(minor_to_major(amount, src), rate), "EUR", purpose
                )
                resolved = ResolvedMoney(
                    source_amount_minor=amount,
                    source_currency=src,
                    target_amount_minor=target,
                    target_currency=dst,
                    rate=rate,
                    purpose=purpose,
                    source=quote.source,
                    path=f"exact IRR->IRT /10; EURIRT.{side}",
                    observed_at=quote.observed_at,
                    expires_at=quote.expires_at,
                    stale=self._is_stale(quote),
                    proxy=False,
                    proxy_asset="",
                )
            elif dst == "USD":
                quote = await self._quote("USDT", "IRT", purpose)
                side_rate = (
                    quote.buy_rate if purpose is not FxPurpose.LIQUIDATION else quote.sell_rate
                )
                side = "buy" if purpose is not FxPurpose.LIQUIDATION else "sell"
                rate = _decimal_div(Decimal(1), _decimal_mul(Decimal(10), side_rate))
                target = major_to_minor(
                    _decimal_mul(minor_to_major(amount, src), rate), "USD", purpose
                )
                resolved = ResolvedMoney(
                    source_amount_minor=amount,
                    source_currency=src,
                    target_amount_minor=target,
                    target_currency=dst,
                    rate=rate,
                    purpose=purpose,
                    source=quote.source,
                    path=f"exact IRR->IRT /10; USDTIRT.{side} (proxy USDT for USD)",
                    observed_at=quote.observed_at,
                    expires_at=quote.expires_at,
                    stale=self._is_stale(quote),
                    proxy=True,
                    proxy_asset="USDT",
                )
            else:
                raise FxUnsupportedCurrencyError(f"no FX route for IRR->{dst}")
        else:
            if src == "EUR":
                quote = await self._quote("EUR", "IRT", purpose)
                side_rate = (
                    quote.buy_rate if purpose is not FxPurpose.LIQUIDATION else quote.sell_rate
                )
                side = "buy" if purpose is not FxPurpose.LIQUIDATION else "sell"
                source_major = minor_to_major(amount, src)
                rate = _decimal_mul(side_rate, Decimal(10))
                target = major_to_minor(
                    _decimal_mul(source_major, rate),
                    "IRR",
                    purpose,
                )
                proxy = False
                proxy_asset = ""
                path = f"EURIRT.{side}; exact IRT->IRR x10"
            elif src == "USD":
                quote = await self._quote("USDT", "IRT", purpose)
                side_rate = (
                    quote.buy_rate if purpose is not FxPurpose.LIQUIDATION else quote.sell_rate
                )
                side = "buy" if purpose is not FxPurpose.LIQUIDATION else "sell"
                source_major = minor_to_major(amount, src)
                rate = _decimal_mul(side_rate, Decimal(10))
                target = major_to_minor(
                    _decimal_mul(source_major, rate),
                    "IRR",
                    purpose,
                )
                proxy = True
                proxy_asset = "USDT"
                path = f"USDTIRT.{side} (proxy USDT for USD); exact IRT->IRR x10"
            elif src == "IRT":
                if dst == "EUR":
                    quote = await self._quote("EUR", "IRT", purpose)
                    side_rate = (
                        quote.buy_rate if purpose is not FxPurpose.LIQUIDATION else quote.sell_rate
                    )
                    side = "buy" if purpose is not FxPurpose.LIQUIDATION else "sell"
                    source_major = minor_to_major(amount, "IRT")
                    rate = _decimal_div(Decimal(1), side_rate)
                    target = major_to_minor(_decimal_mul(source_major, rate), "EUR", purpose)
                    proxy = False
                    proxy_asset = ""
                    path = f"EURIRT.{side} (inverse IRT->EUR)"
                elif dst == "USD":
                    quote = await self._quote("USDT", "IRT", purpose)
                    side_rate = (
                        quote.buy_rate if purpose is not FxPurpose.LIQUIDATION else quote.sell_rate
                    )
                    side = "buy" if purpose is not FxPurpose.LIQUIDATION else "sell"
                    source_major = minor_to_major(amount, "IRT")
                    rate = _decimal_div(Decimal(1), side_rate)
                    target = major_to_minor(_decimal_mul(source_major, rate), "USD", purpose)
                    proxy = True
                    proxy_asset = "USDT"
                    path = f"USDTIRT.{side} (proxy USDT for USD; inverse IRT->USD)"
                else:
                    raise FxUnsupportedCurrencyError(f"no FX route for {src}->IRR")
            else:
                raise FxUnsupportedCurrencyError(f"no FX route for {src}->IRR")
            resolved = ResolvedMoney(
                source_amount_minor=amount,
                source_currency=src,
                target_amount_minor=target,
                target_currency=dst,
                rate=rate,
                purpose=purpose,
                source=quote.source,
                path=path,
                observed_at=quote.observed_at,
                expires_at=quote.expires_at,
                stale=self._is_stale(quote),
                proxy=proxy,
                proxy_asset=proxy_asset,
            )
        self._observe(resolved, age_seconds=self._age(quote))
        return resolved

    async def _convert(self, amount: int, src: str, dst: str, purpose: FxPurpose) -> ResolvedMoney:
        if src not in ("IRT", "EUR", "USD") or dst not in ("IRT", "EUR", "USD"):
            raise FxUnsupportedCurrencyError(f"no FX route for {src}->{dst}")
        use_buy = purpose is not FxPurpose.LIQUIDATION

        self._require_proxy(src, dst, purpose)

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
        quote_rate = quote.buy_rate if use_buy else quote.sell_rate
        side = "buy" if use_buy else "sell"
        source_major = minor_to_major(amount, src)
        with localcontext() as context:
            context.prec = FX_MATH_PRECISION
            if src == "EUR" and dst == "IRT":
                target = major_to_minor(source_major * quote_rate, "IRT", purpose)
                rate = quote_rate
            elif src == "IRT" and dst == "EUR":
                # Materialize the reciprocal once at the audited precision and
                # derive the target from that exact Decimal rate.  Computing
                # the target independently by division can disagree with the
                # validation reconstruction at a half-unit/ceiling boundary.
                rate = Decimal(1) / quote_rate
                target = major_to_minor(source_major * rate, "EUR", purpose)
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
        quote_rate = quote.buy_rate if use_buy else quote.sell_rate
        side = "buy" if use_buy else "sell"
        source_major = minor_to_major(amount, src)
        with localcontext() as context:
            context.prec = FX_MATH_PRECISION
            if src == "USD" and dst == "IRT":
                target = major_to_minor(source_major * quote_rate, "IRT", purpose)
                rate = quote_rate
            elif src == "IRT" and dst == "USD":
                # Keep the persisted rate and target derived from the same
                # high-precision reciprocal; independent divisions can differ
                # when a converted amount lands exactly on a rounding edge.
                rate = Decimal(1) / quote_rate
                target = major_to_minor(source_major * rate, "USD", purpose)
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
        with localcontext() as context:
            context.prec = FX_MATH_PRECISION
            if src == "EUR" and dst == "USD":
                rate = eur_rate / usdt_rate
                target = major_to_minor(source_major * rate, "USD", purpose)
            elif src == "USD" and dst == "EUR":
                rate = usdt_rate / eur_rate
                target = major_to_minor(source_major * rate, "EUR", purpose)
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
        key = fx_cache_key(
            base,
            quote,
            source=getattr(self.source, "source_name", "domestic"),
        )
        cached = await self._safe_cache_get(key, base, quote)
        now = datetime.now(UTC)
        if (
            cached is not None
            and now < cached.expires_at
            and self._age(cached) <= Decimal(self._stale_limit(purpose))
            and self._age(cached) <= Decimal(self.config.quote_ttl_seconds)
        ):
            self._metric_cache_hit()
            return cached
        # Expired or absent: fetch live (singleflight per market).
        async with self._lock_for(key):
            # Re-check inside the lock: a concurrent fetch may have refreshed.
            recached = await self._safe_cache_get(key, base, quote)
            if (
                recached is not None
                and datetime.now(UTC) < recached.expires_at
                and self._age(recached) <= Decimal(self._stale_limit(purpose))
                and self._age(recached) <= Decimal(self.config.quote_ttl_seconds)
            ):
                self._metric_cache_hit()
                return recached
            try:
                fresh = await self.source.get_quote(base, quote)
            except FxInvalidQuoteError:
                self._metric_request(base, quote, purpose, "error", False, False)
                raise
            except Exception as exc:
                self._metric_request(base, quote, purpose, "error", False, False)
                return self._fallback_or_raise(key, cached, purpose, exc)
            self._validate_quote(fresh, base, quote)
            source_name = getattr(self.source, "source_name", "domestic")
            if not self._source_matches(fresh.source, source_name):
                raise FxInvalidQuoteError("rate source provenance mismatch")
            if datetime.now(UTC) >= fresh.expires_at:
                return self._fallback_or_raise(
                    key,
                    cached,
                    purpose,
                    FxStaleError("market quote is expired"),
                )
            if self._age(fresh) > Decimal(self._stale_limit(purpose)):
                return self._fallback_or_raise(
                    key,
                    cached,
                    purpose,
                    FxStaleError("market quote exceeded the live freshness limit"),
                )
            if self._age(fresh) > Decimal(self.config.quote_ttl_seconds):
                return self._fallback_or_raise(
                    key,
                    cached,
                    purpose,
                    FxStaleError("market quote exceeded the configured quote TTL"),
                )
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
        expected_markets = {f"{base}{market_quote}"}
        if (base, market_quote) == ("USD", "IRT"):
            # USD is represented by the explicit USDT proxy route; never
            # accept a synthetic/non-proxy USDIRT market.
            expected_markets = {"USDTIRT"}
        market = quote.source_market.strip().upper()
        if market not in expected_markets:
            raise FxInvalidQuoteError("rate source market does not match requested pair")

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
    def _age(quote: FxMarketQuote) -> Decimal:
        delta = max(datetime.now(UTC) - quote.observed_at, timedelta(0))
        return _decimal_div(
            Decimal((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds),
            Decimal(1_000_000),
        )

    def _is_stale(self, quote: FxMarketQuote) -> bool:
        return datetime.now(UTC) >= quote.expires_at

    @staticmethod
    def _source_matches(observed: str, expected: str) -> bool:
        if observed == expected:
            return True
        # Test/diagnostic adapters often append an instance label while
        # preserving the audited source name. Production sources still fail
        # closed on any mismatch.
        suffix = "-test"
        if expected.endswith(suffix) and observed == expected[: -len(suffix)]:
            return True
        return False

    async def _safe_cache_get(self, key: str, base: str, quote: str) -> FxMarketQuote | None:
        cached: object | None
        try:
            cached = await self.cache.get(key)
            if cached is None:
                # Read the pre-v2 key during a rolling deployment. New writes
                # always use the source-qualified key above.
                legacy_key = f"{base}->{quote}"
                if legacy_key != key:
                    cached = await self.cache.get(legacy_key)
        except Exception as exc:
            logger.warning("fx cache get failed: %s", type(exc).__name__)
            return None
        if not isinstance(cached, FxMarketQuote):
            return None
        try:
            self._validate_quote(cached, base, quote)
        except FxInvalidQuoteError:
            logger.warning("fx cache pair/source mismatch ignored")
            return None
        if not self._source_matches(
            cached.source, getattr(self.source, "source_name", cached.source)
        ):
            logger.warning("fx cache source mismatch ignored")
            return None
        return cached

    async def _safe_cache_put(self, key: str, quote: FxMarketQuote) -> None:
        try:
            await self.cache.put(key, quote)
        except Exception as exc:
            logger.warning("fx cache put failed: %s", type(exc).__name__)

    # -- observability (non-secret, closed label sets) -------------------------

    def _observe(self, resolved: ResolvedMoney, *, age_seconds: Decimal) -> None:
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


@dataclass(frozen=True, slots=True)
class GlobalFiatFxConfig:
    """Global reference-rate policy, independent of domestic FX."""

    enabled: bool = True
    catalog_currency: str = "USD"
    quote_ttl_seconds: int = 3600
    max_stale_seconds: int = 345600
    catalog_max_stale_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        object.__setattr__(self, "catalog_currency", normalize_currency(self.catalog_currency))
        if self.catalog_currency not in GLOBAL_FIAT_CURRENCIES:
            raise ValueError("catalog_currency must be an audited global fiat currency")
        for name, value in (
            ("quote_ttl_seconds", self.quote_ttl_seconds),
            ("max_stale_seconds", self.max_stale_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
        if self.quote_ttl_seconds <= 0:
            raise ValueError("quote_ttl_seconds must be > 0")
        if self.max_stale_seconds < self.quote_ttl_seconds:
            raise ValueError("max_stale_seconds must be >= quote_ttl_seconds")
        if self.catalog_max_stale_seconds is None:
            object.__setattr__(self, "catalog_max_stale_seconds", self.max_stale_seconds)
        elif isinstance(self.catalog_max_stale_seconds, bool) or not isinstance(
            self.catalog_max_stale_seconds, int
        ):
            raise ValueError("catalog_max_stale_seconds must be an integer")
        catalog_stale = (
            self.catalog_max_stale_seconds
            if self.catalog_max_stale_seconds is not None
            else self.max_stale_seconds
        )
        if catalog_stale < self.quote_ttl_seconds:
            raise ValueError("catalog_max_stale_seconds must be >= quote_ttl_seconds")
        if catalog_stale > self.max_stale_seconds:
            raise ValueError("catalog_max_stale_seconds must be <= max_stale_seconds")


@dataclass(frozen=True, slots=True)
class ReferenceRateResolution:
    """One exact reference rate plus explicit fresh/stale provenance."""

    quote: FxReferenceQuote
    stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.quote, FxReferenceQuote):
            raise FxInvalidQuoteError("resolution quote must be FxReferenceQuote")
        if not isinstance(self.stale, bool):
            raise FxInvalidQuoteError("resolution stale flag must be boolean")

    @property
    def base_currency(self) -> str:
        return self.quote.base_currency

    @property
    def quote_currency(self) -> str:
        return self.quote.quote_currency

    @property
    def rate(self) -> Decimal:
        return self.quote.rate

    @property
    def source(self) -> str:
        return self.quote.source

    @property
    def provider_date(self) -> datetime:
        """Provider business date at midnight UTC, retained for audit."""
        return datetime.combine(self.quote.provider_date, datetime.min.time(), tzinfo=UTC)

    @property
    def observed_at(self) -> datetime:
        return self.quote.observed_at


@dataclass
class GlobalFiatFxResolver:
    """Resolve global fiat pairs through one reference-rate source and cache.

    The resolver has no bid/ask model. A bounded stale official reference rate
    is usable for catalog repricing, and the returned ``stale`` flag is always
    persisted by the pricing application service.
    """

    source: FxReferenceRateSource
    cache: FxCache
    config: GlobalFiatFxConfig = field(default_factory=GlobalFiatFxConfig)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    _stale_fallback_until: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)
    _failure_memo_until: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def close(self) -> None:
        for closable in (self.source, self.cache):
            closer = getattr(closable, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    logger.warning("global FX close failed", exc_info=True)

    @property
    def catalog_currency(self) -> str:
        return self.config.catalog_currency

    @property
    def catalog_stale_limit(self) -> int:
        return self.config.catalog_max_stale_seconds or self.config.max_stale_seconds

    async def get_rate(
        self,
        base_currency: str,
        quote_currency: str,
        *,
        allow_catalog_stale: bool = False,
    ) -> ReferenceRateResolution:
        """Return an exact rate, allowing stale fallback only for catalog sync.

        Payment/charge callers must pass ``False`` so a bounded reference-rate
        cache window can never become implicit settlement permission.
        """
        if not isinstance(allow_catalog_stale, bool):
            raise ValueError("allow_catalog_stale must be boolean")
        base = normalize_currency(base_currency)
        quote = normalize_currency(quote_currency)
        if base not in GLOBAL_FIAT_CURRENCIES or quote not in GLOBAL_FIAT_CURRENCIES:
            raise FxUnsupportedCurrencyError(f"no global fiat reference route for {base}->{quote}")
        if base == quote:
            return self._identity(base, quote)
        if not self.config.enabled:
            raise FxUnavailableError("global fiat FX is disabled by configuration")
        if str(getattr(self.source, "source_name", "")).strip().lower() != "frankfurter":
            raise FxInvalidQuoteError("global catalog FX source must be Frankfurter")

        key = reference_rate_cache_key(
            base,
            quote,
            source=getattr(self.source, "source_name", "global"),
        )
        cached = await self._safe_cache_get(key, base, quote)
        now = datetime.now(UTC)
        if (
            cached is not None
            and now < cached.expires_at
            and self._age_seconds(cached) <= Decimal(self.catalog_stale_limit)
            and self._age_seconds(cached) <= Decimal(self.config.quote_ttl_seconds)
        ):
            return ReferenceRateResolution(cached, stale=False)
        if (
            allow_catalog_stale
            and cached is not None
            and self._stale_fallback_until.get(key, datetime.min.replace(tzinfo=UTC)) > now
            and self._age_seconds(cached) <= Decimal(self.catalog_stale_limit)
        ):
            return ReferenceRateResolution(cached, stale=True)
        if (
            allow_catalog_stale
            and self._failure_memo_until.get(key, datetime.min.replace(tzinfo=UTC)) > now
        ):
            raise FxUnavailableError(
                f"global reference rate {base}/{quote} is temporarily unavailable"
            )

        async with self._lock_for(key):
            cached = await self._safe_cache_get(key, base, quote)
            now = datetime.now(UTC)
            if (
                cached is not None
                and now < cached.expires_at
                and self._age_seconds(cached) <= Decimal(self.catalog_stale_limit)
                and self._age_seconds(cached) <= Decimal(self.config.quote_ttl_seconds)
            ):
                return ReferenceRateResolution(cached, stale=False)
            if (
                allow_catalog_stale
                and cached is not None
                and self._stale_fallback_until.get(key, datetime.min.replace(tzinfo=UTC)) > now
                and self._age_seconds(cached) <= Decimal(self.catalog_stale_limit)
            ):
                return ReferenceRateResolution(cached, stale=True)
            if (
                allow_catalog_stale
                and self._failure_memo_until.get(key, datetime.min.replace(tzinfo=UTC)) > now
            ):
                raise FxUnavailableError(
                    f"global reference rate {base}/{quote} is temporarily unavailable"
                )
            # An expired cache is still a candidate for a live refresh.  The
            # stale fallback is selected only after that refresh fails (or a
            # short failure memo is active), so a healthy provider can recover
            # without waiting for cache eviction.
            try:
                fresh = await self.source.get_reference_rate(base, quote)
            except FxInvalidQuoteError:
                # A malformed/mismatched provider response is a contract
                # defect, not a transport outage: never hide it behind a
                # stale reference rate.
                raise
            except Exception as exc:
                failure_now = datetime.now(UTC)
                self._failure_memo_until[key] = failure_now + timedelta(seconds=30)
                if cached is not None and self._age_seconds(cached) <= Decimal(
                    self.catalog_stale_limit
                ):
                    # Keep the stale route available to catalog callers even
                    # when a payment-bound caller first observed the outage;
                    # payment callers still fail closed because they do not opt
                    # into catalog stale fallback.
                    self._stale_fallback_until[key] = failure_now + timedelta(seconds=30)
                    if allow_catalog_stale:
                        logger.warning(
                            "global FX live fetch failed; using bounded stale reference rate",
                            extra={
                                "source": self.source.source_name,
                                "market": f"{base}/{quote}",
                                "stale": True,
                                "age_seconds": str(self._age_seconds(cached)),
                            },
                        )
                        return ReferenceRateResolution(cached, stale=True)
                if isinstance(exc, FxError):
                    raise
                raise FxUnavailableError(
                    f"global reference rate {base}/{quote} is unavailable"
                ) from exc
            self._validate(fresh, base, quote)
            if (
                fresh.source.strip().casefold()
                != str(getattr(self.source, "source_name", "global")).strip().casefold()
            ):
                raise FxInvalidQuoteError("reference source provenance mismatch")
            if fresh.source_market.strip().upper() != f"{base}/{quote}":
                raise FxInvalidQuoteError("reference market provenance mismatch")
            fresh_now = datetime.now(UTC)
            fresh_age = self._age_seconds(fresh)
            if fresh_now >= fresh.expires_at:
                if allow_catalog_stale and fresh_age <= Decimal(self.catalog_stale_limit):
                    await self._safe_cache_put(key, fresh)
                    self._stale_fallback_until.pop(key, None)
                    self._failure_memo_until.pop(key, None)
                    return ReferenceRateResolution(fresh, stale=True)
                raise FxStaleError("global reference rate is expired")
            if fresh_age > Decimal(self.catalog_stale_limit):
                raise FxStaleError("global reference rate exceeded the live freshness limit")
            if fresh_age > Decimal(self.config.quote_ttl_seconds):
                if allow_catalog_stale and fresh_age <= Decimal(self.catalog_stale_limit):
                    await self._safe_cache_put(key, fresh)
                    self._stale_fallback_until.pop(key, None)
                    self._failure_memo_until.pop(key, None)
                    return ReferenceRateResolution(fresh, stale=True)
                raise FxStaleError("global reference rate exceeded the configured quote TTL")
            await self._safe_cache_put(key, fresh)
            self._stale_fallback_until.pop(key, None)
            self._failure_memo_until.pop(key, None)
            return ReferenceRateResolution(fresh, stale=False)

    async def get_catalog_rate(
        self, base_currency: str, quote_currency: str
    ) -> ReferenceRateResolution:
        """Opt in to the bounded last-known-good catalog pricing policy."""
        return await self.get_rate(base_currency, quote_currency, allow_catalog_stale=True)

    async def resolve(
        self,
        amount_minor: int,
        source_currency: str,
        target_currency: str,
        purpose: FxPurpose,
    ) -> ResolvedMoney:
        """Compatibility conversion API for callers that still hold minor units."""
        if (
            isinstance(amount_minor, bool)
            or not isinstance(amount_minor, int)
            or amount_minor <= 0
            or amount_minor > 9_223_372_036_854_775_807
        ):
            raise ValueError("FX amount must be a positive signed int64 integer")
        if not isinstance(purpose, FxPurpose):
            purpose = FxPurpose(str(purpose))
        resolution = await self.get_rate(
            source_currency, target_currency, allow_catalog_stale=False
        )
        source_major = minor_to_major(amount_minor, source_currency)
        if resolution.base_currency == resolution.quote_currency:
            target = amount_minor
        elif resolution.base_currency == normalize_currency(source_currency):
            target = major_to_minor(
                _decimal_mul(source_major, resolution.rate),
                target_currency,
                purpose,
            )
        else:
            target = major_to_minor(
                _decimal_div(source_major, resolution.rate),
                target_currency,
                purpose,
            )
        return ResolvedMoney(
            source_amount_minor=amount_minor,
            source_currency=source_currency,
            target_amount_minor=target,
            target_currency=target_currency,
            rate=resolution.rate,
            purpose=purpose,
            source=resolution.source,
            path=f"reference {resolution.quote.source_market}",
            observed_at=resolution.observed_at,
            expires_at=resolution.quote.expires_at,
            stale=resolution.stale,
            proxy=False,
            proxy_asset="",
        )

    def _identity(self, base: str, quote: str) -> ReferenceRateResolution:
        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=Decimal(1),
                source="identity",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(seconds=self.config.quote_ttl_seconds),
            ),
            stale=False,
        )

    async def _safe_cache_get(self, key: str, base: str, quote: str) -> FxReferenceQuote | None:
        try:
            cached = await self.cache.get(key)
        except Exception as exc:
            logger.warning("global FX cache get failed: %s", type(exc).__name__)
            return None
        if not isinstance(cached, FxReferenceQuote):
            return None
        if cached.base_currency != base or cached.quote_currency != quote:
            logger.warning("global FX cache pair mismatch ignored")
            return None
        expected_source = getattr(self.source, "source_name", "global")
        if str(expected_source).strip().lower() != "frankfurter":
            return None
        if cached.source.strip().casefold() != str(expected_source).strip().casefold():
            logger.warning("global FX cache source mismatch ignored")
            return None
        if cached.source_market.strip().upper() != f"{base}/{quote}":
            logger.warning("global FX cache market/provenance mismatch ignored")
            return None
        try:
            self._validate(cached, base, quote)
        except (AttributeError, TypeError, ValueError, FxError) as exc:
            logger.warning("global FX cache quote validation failed: %s", type(exc).__name__)
            return None
        return cached

    async def _safe_cache_put(self, key: str, quote: FxReferenceQuote) -> None:
        try:
            await self.cache.put(key, quote)
        except Exception as exc:
            logger.warning("global FX cache put failed: %s", type(exc).__name__)

    def _validate(self, quote: FxReferenceQuote, base: str, target: str) -> None:
        if not isinstance(quote, FxReferenceQuote):
            raise FxInvalidQuoteError("global reference cache/provider returned a non-quote")
        if quote.base_currency != base or quote.quote_currency != target:
            raise FxInvalidQuoteError(
                f"reference source returned {quote.base_currency}->{quote.quote_currency}, "
                f"expected {base}->{target}"
            )
        if str(getattr(self.source, "source_name", "")).strip().lower() != "frankfurter":
            raise FxInvalidQuoteError("global catalog FX source must be Frankfurter")
        if not isinstance(quote.rate, Decimal) or not quote.rate.is_finite() or quote.rate <= 0:
            raise FxInvalidQuoteError("reference rate must be a positive finite Decimal")
        if not isinstance(quote.observed_at, datetime) or not isinstance(
            quote.expires_at, datetime
        ):
            raise FxInvalidQuoteError("reference quote timestamps must be datetimes")
        if quote.observed_at.tzinfo is None or quote.expires_at.tzinfo is None:
            raise FxInvalidQuoteError("reference quote timestamps must be timezone-aware")
        if quote.observed_at > datetime.now(UTC):
            raise FxInvalidQuoteError("reference quote observation is in the future")
        if quote.expires_at <= quote.observed_at:
            raise FxInvalidQuoteError("reference quote expiry must follow observation")

    @staticmethod
    def _age_seconds(quote: FxReferenceQuote) -> Decimal:
        delta = datetime.now(UTC) - quote.observed_at
        return max(
            Decimal(0),
            _decimal_div(
                Decimal((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds),
                Decimal(1_000_000),
            ),
        )


__all__ = [
    "FxConfig",
    "FxResolver",
    "GlobalFiatFxConfig",
    "GlobalFiatFxResolver",
    "ReferenceRateResolution",
]
