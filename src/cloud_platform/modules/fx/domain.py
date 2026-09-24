"""Currency / FX domain: immutable value objects and money rules.

This is provider-neutral platform code. It knows currencies, minor-unit
exponents, conversion purposes and the shape of a resolved conversion — but
it never touches the network, Telegram, FastAPI, SQLAlchemy or Redis. The
live rate comes from a :class:`FxRateSource` port (AbanTether is one
implementation); routing and rounding live in the resolver service.

Money rules (audited in one place):

- IRT and IRR are zero-decimal: ``amount_minor`` IS the Toman / Rial count.
  ``1 IRT = 10 IRR`` exactly, no FX provider call required.
- EUR and USD are 2-decimal: ``amount_minor`` is cents.
- All rate arithmetic uses :class:`Decimal`; float never appears.
- CHARGE conversions round UP (ceiling) so the customer is never
  undercharged; DISPLAY uses half-up; LIQUIDATION uses floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
    InvalidOperation,
    localcontext,
)
from enum import StrEnum


class FxPurpose(StrEnum):
    """Why the caller is converting money (selects bid/ask + rounding)."""

    DISPLAY = "display"
    CHARGE = "charge"
    LIQUIDATION = "liquidation"


class FxError(Exception):
    """Base error for FX resolution failures (fail closed)."""


class FxUnavailableError(FxError):
    """No usable rate: missing/inactive market, malformed rate, no route."""


class FxStaleError(FxError):
    """Only a too-old quote was available and the purpose forbids it."""


class FxProxyNotAllowedError(FxError):
    """A USD/USDT proxy route was required but is disabled for this purpose."""


class FxUnsupportedCurrencyError(FxError):
    """No conversion route exists for this currency pair."""


class FxInvalidQuoteError(FxError):
    """A rate source returned a structurally invalid quote."""


#: Audited ISO-4217 minor-unit exponents. Unknown currencies fail closed in
#: financial conversion instead of silently assuming two decimal places.
#: Zero-decimal currencies store the major unit directly (for example JPY 691
#: means ¥691, not ¥6.91). IRT/IRR are platform domestic units and follow the
#: same zero-decimal rule.
#: Shared working precision for every materialization and validation of a
#: resolved conversion.  Keeping one context is important for non-terminating
#: reciprocals: computing the target at 50 digits and validating it at 80 can
#: move a half-cent DISPLAY value across the rounding boundary.
FX_MATH_PRECISION = 512

CURRENCY_EXPONENTS: dict[str, int] = {
    "IRT": 0,
    "IRR": 0,
    "JPY": 0,
    "KRW": 0,
    "EUR": 2,
    "GBP": 2,
    "SGD": 2,
    "AUD": 2,
    "CAD": 2,
    "USD": 2,
}

#: International fiat currencies currently observed from foreign providers.
#: This is a currency-family routing set, not a provider-name branch. Adding a
#: future ISO currency is an explicit audited code change.
GLOBAL_FIAT_CURRENCIES: frozenset[str] = frozenset(
    {"EUR", "GBP", "JPY", "SGD", "AUD", "CAD", "USD", "KRW"}
)

#: Currencies the resolver explicitly supports (routing is defined for these).
SUPPORTED_CURRENCIES: frozenset[str] = frozenset(CURRENCY_EXPONENTS)


def normalize_currency(currency: str) -> str:
    """Upper-case asset code or raise.

    Wallet/c settlement currencies are 3-letter ISO codes; FX market base
    assets include 4-letter codes (USDT as the explicit USD proxy). Both
    pass through here; the resolver enforces which pairs are convertible.
    """
    if not isinstance(currency, str):
        raise FxUnsupportedCurrencyError(f"unsupported currency {currency!r}")
    code = currency.strip().upper()
    if not (2 <= len(code) <= 5) or not code.isalpha():
        raise FxUnsupportedCurrencyError(f"unsupported currency {currency!r}")
    return code


def currency_exponent(currency: str) -> int:
    """Audited decimals for ``currency``; unknown ISO codes fail closed."""
    code = normalize_currency(currency)
    try:
        return CURRENCY_EXPONENTS[code]
    except KeyError as exc:
        raise FxUnsupportedCurrencyError(
            f"minor-unit exponent is not audited for currency {code!r}"
        ) from exc


def minor_to_major(amount_minor: int, currency: str) -> Decimal:
    """Integer minor units -> major Decimal (no float)."""
    if (
        isinstance(amount_minor, bool)
        or not isinstance(amount_minor, int)
        or not -(2**63) <= amount_minor <= 2**63 - 1
    ):
        raise ValueError("amount_minor must be a signed 64-bit integer")
    exp = currency_exponent(currency)
    with localcontext() as context:
        context.prec = max(50, len(str(abs(amount_minor))) + exp + 20)
        return Decimal(amount_minor) / (Decimal(10) ** exp)


def _rounding_for(purpose: FxPurpose) -> str:
    if purpose is FxPurpose.CHARGE:
        return ROUND_CEILING
    if purpose is FxPurpose.LIQUIDATION:
        return ROUND_FLOOR
    return ROUND_HALF_UP


def major_to_minor(amount_major: Decimal, currency: str, purpose: FxPurpose) -> int:
    """Major Decimal -> integer minor units with purpose-directed rounding."""
    if not isinstance(amount_major, Decimal):
        raise ValueError("amount_major must be a Decimal (never float)")
    exp = currency_exponent(currency)
    quantum = Decimal(1) if exp == 0 else Decimal(1).scaleb(-exp)
    with localcontext() as context:
        context.prec = max(50, len(amount_major.as_tuple().digits) + exp + 20)
        quantized = amount_major.quantize(quantum, rounding=_rounding_for(purpose))
        minor = int(quantized * (Decimal(10) ** exp))
        if not -(2**63) <= minor <= 2**63 - 1:
            raise ValueError("converted amount is outside signed 64-bit bounds")
        return minor


def _validate_quote_timestamps(observed_at: datetime, expires_at: datetime) -> None:
    """Reject cache/source timestamps that cannot express freshness safely."""
    if not isinstance(observed_at, datetime) or not isinstance(expires_at, datetime):
        raise FxInvalidQuoteError("quote timestamps must be datetime values")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise FxInvalidQuoteError("quote observed_at must be timezone-aware")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise FxInvalidQuoteError("quote expires_at must be timezone-aware")
    if expires_at <= observed_at:
        raise FxInvalidQuoteError("quote expires_at must be after observed_at")
    if observed_at > datetime.now(UTC):
        raise FxInvalidQuoteError("quote observed_at must not be in the future")


def parse_rate(raw: object, *, field: str) -> Decimal:
    """Parse a provider rate string into a positive finite Decimal."""
    if isinstance(raw, (bool, float)):
        raise FxInvalidQuoteError(f"malformed rate field {field!r}")
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise FxInvalidQuoteError(f"malformed rate field {field!r}") from exc
    if not value.is_finite() or value <= 0:
        raise FxInvalidQuoteError(f"non-positive or non-finite rate field {field!r}")
    return value


@dataclass(frozen=True, slots=True)
class FxMarketQuote:
    """One validated market quote from a rate source.

    For ``EURIRT``: ``base_currency="EUR"``, ``quote_currency="IRT"``,
    ``buy_rate`` is IRT per 1 EUR to BUY EUR (acquisition side, used for
    customer CHARGE), ``sell_rate`` is IRT per 1 EUR when SELLING EUR.
    """

    base_currency: str
    quote_currency: str
    buy_rate: Decimal
    sell_rate: Decimal
    source: str
    source_market: str
    observed_at: datetime
    expires_at: datetime
    proxy: bool = False
    proxy_asset: str = ""

    def __post_init__(self) -> None:
        base = normalize_currency(self.base_currency)
        quote = normalize_currency(self.quote_currency)
        object.__setattr__(self, "base_currency", base)
        object.__setattr__(self, "quote_currency", quote)
        if (
            not isinstance(self.buy_rate, Decimal)
            or not self.buy_rate.is_finite()
            or self.buy_rate <= 0
        ):
            raise FxInvalidQuoteError("buy_rate must be a positive finite Decimal")
        if (
            not isinstance(self.sell_rate, Decimal)
            or not self.sell_rate.is_finite()
            or self.sell_rate <= 0
        ):
            raise FxInvalidQuoteError("sell_rate must be a positive finite Decimal")
        if not self.source or not self.source.strip():
            raise FxInvalidQuoteError("source must not be empty")
        if not self.source_market or not self.source_market.strip():
            raise FxInvalidQuoteError("source_market must not be empty")
        if not isinstance(self.proxy, bool):
            raise FxInvalidQuoteError("proxy must be a boolean")
        if not isinstance(self.proxy_asset, str):
            raise FxInvalidQuoteError("proxy_asset must be text")
        if self.proxy != bool(self.proxy_asset.strip()):
            raise FxInvalidQuoteError("proxy_asset must be present exactly when proxy is true")
        _validate_quote_timestamps(self.observed_at, self.expires_at)


@dataclass(frozen=True, slots=True)
class FxReferenceQuote:
    """One official reference rate with explicit no-spread semantics.

    Unlike :class:`FxMarketQuote`, this contract has exactly one rate. It must
    never be presented as a provider bid/ask quote. ``provider_date`` is the
    business date published by the source; ``observed_at`` is when this
    process retrieved and validated it, and therefore the freshness clock used
    during weekends and holidays.
    """

    base_currency: str
    quote_currency: str
    rate: Decimal
    source: str
    source_market: str
    provider_date: date
    observed_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_currency", normalize_currency(self.base_currency))
        object.__setattr__(self, "quote_currency", normalize_currency(self.quote_currency))
        if not isinstance(self.rate, Decimal):
            raise FxInvalidQuoteError("reference_rate must be a Decimal (never float)")
        parse_rate(self.rate, field="reference_rate")
        if not isinstance(self.provider_date, date) or isinstance(self.provider_date, datetime):
            raise FxInvalidQuoteError("provider_date must be a date value")
        if self.provider_date > datetime.now(UTC).date():
            raise FxInvalidQuoteError("provider_date must not be in the future")
        if not self.source or not self.source.strip():
            raise FxInvalidQuoteError("reference source must not be empty")
        if not self.source_market or not self.source_market.strip():
            raise FxInvalidQuoteError("reference source_market must not be empty")
        _validate_quote_timestamps(self.observed_at, self.expires_at)


@dataclass(frozen=True, slots=True)
class ResolvedMoney:
    """One completed conversion with full audit metadata."""

    source_amount_minor: int
    source_currency: str
    target_amount_minor: int
    target_currency: str
    rate: Decimal
    purpose: FxPurpose
    source: str
    path: str
    observed_at: datetime
    expires_at: datetime
    stale: bool = False
    proxy: bool = False
    proxy_asset: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_currency", normalize_currency(self.source_currency))
        object.__setattr__(self, "target_currency", normalize_currency(self.target_currency))
        if (
            isinstance(self.source_amount_minor, bool)
            or not isinstance(self.source_amount_minor, int)
            or self.source_amount_minor < 0
            or self.source_amount_minor > 2**63 - 1
        ):
            raise ValueError("source_amount_minor must be a non-negative int")
        if (
            isinstance(self.target_amount_minor, bool)
            or not isinstance(self.target_amount_minor, int)
            or self.target_amount_minor < 0
            or self.target_amount_minor > 2**63 - 1
        ):
            raise ValueError("target_amount_minor must be a non-negative int")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite() or self.rate <= 0:
            raise ValueError("rate must be a positive finite Decimal")
        if (
            len(self.rate.as_tuple().digits) > FX_MATH_PRECISION
            or abs(int(self.rate.as_tuple().exponent)) > 1000
        ):
            raise ValueError("rate exceeds the audited numeric range")
        if not isinstance(self.purpose, FxPurpose):
            raise ValueError("purpose must be an FxPurpose")
        if not self.source.strip() or not self.path.strip():
            raise ValueError("resolved FX provenance must be non-empty")
        with localcontext() as context:
            source_major = minor_to_major(self.source_amount_minor, self.source_currency)
            context.prec = FX_MATH_PRECISION
            expected_target = major_to_minor(
                source_major * self.rate,
                self.target_currency,
                self.purpose,
            )
        if expected_target != self.target_amount_minor:
            raise ValueError("resolved FX amounts do not match the exact rate")
        if not isinstance(self.proxy, bool):
            raise ValueError("proxy must be boolean")
        if not isinstance(self.stale, bool):
            raise ValueError("stale must be boolean")
        normalized_asset = (
            self.proxy_asset.strip().upper() if isinstance(self.proxy_asset, str) else ""
        )
        if self.proxy and normalized_asset != "USDT":
            raise ValueError("proxy conversion must identify the audited USDT asset")
        if not self.proxy and normalized_asset:
            raise ValueError("non-proxy conversion must not identify a proxy asset")
        object.__setattr__(self, "proxy_asset", normalized_asset)
        _validate_quote_timestamps(self.observed_at, self.expires_at)


@dataclass(frozen=True, slots=True)
class ConversionSnapshot:
    """Immutable, persistable record of a financially binding conversion.

    Stored on the payment session at recharge creation so the callback and
    reconciliation NEVER fetch a new rate: the wallet credit is frozen.
    """

    source_amount_minor: int
    source_currency: str
    target_amount_minor: int
    target_currency: str
    rate: Decimal
    purpose: FxPurpose
    source: str
    path: str
    observed_at: datetime
    proxy: bool = False
    proxy_asset: str = ""
    expires_at: datetime | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        source = normalize_currency(self.source_currency)
        target = normalize_currency(self.target_currency)
        object.__setattr__(self, "source_currency", source)
        object.__setattr__(self, "target_currency", target)
        for name, value in (
            ("source_amount_minor", self.source_amount_minor),
            ("target_amount_minor", self.target_amount_minor),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 2**63 - 1
            ):
                raise FxInvalidQuoteError(f"{name} must be a non-negative integer")
        if not isinstance(self.rate, Decimal) or not self.rate.is_finite() or self.rate <= 0:
            raise FxInvalidQuoteError("conversion snapshot rate must be a positive finite Decimal")
        if (
            len(self.rate.as_tuple().digits) > FX_MATH_PRECISION
            or abs(int(self.rate.as_tuple().exponent)) > 1000
        ):
            raise FxInvalidQuoteError("conversion snapshot rate exceeds the audited numeric range")
        if not isinstance(self.purpose, FxPurpose):
            raise FxInvalidQuoteError("conversion snapshot purpose is invalid")
        try:
            with localcontext() as context:
                source_major = minor_to_major(self.source_amount_minor, self.source_currency)
                context.prec = FX_MATH_PRECISION
                expected_target = major_to_minor(
                    source_major * self.rate,
                    self.target_currency,
                    self.purpose,
                )
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise FxInvalidQuoteError(
                "conversion snapshot amounts are outside the audited numeric range"
            ) from exc
        if expected_target != self.target_amount_minor:
            raise FxInvalidQuoteError("conversion snapshot amounts do not match the exact rate")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise FxInvalidQuoteError("conversion snapshot observed_at must be timezone-aware")
        if self.expires_at is not None and (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at <= self.observed_at
        ):
            raise FxInvalidQuoteError("conversion snapshot expiry must be a later aware datetime")
        if self.purpose in {FxPurpose.CHARGE, FxPurpose.LIQUIDATION} and self.expires_at is None:
            raise FxInvalidQuoteError("binding FX snapshots require an expiry timestamp")
        if not isinstance(self.stale, bool):
            raise FxInvalidQuoteError("conversion snapshot stale flag must be boolean")
        if not self.source.strip() or not self.path.strip():
            raise FxInvalidQuoteError("conversion snapshot provenance must be non-empty")
        if not isinstance(self.proxy, bool):
            raise FxInvalidQuoteError("conversion snapshot proxy must be boolean")
        normalized_asset = (
            self.proxy_asset.strip().upper() if isinstance(self.proxy_asset, str) else ""
        )
        if self.proxy and normalized_asset != "USDT":
            raise FxInvalidQuoteError("proxy conversion must identify the audited USDT asset")
        if not self.proxy and normalized_asset:
            raise FxInvalidQuoteError("non-proxy conversion must not identify a proxy asset")
        object.__setattr__(self, "proxy_asset", normalized_asset)

    @classmethod
    def from_resolved(cls, resolved: ResolvedMoney) -> ConversionSnapshot:
        return cls(
            source_amount_minor=resolved.source_amount_minor,
            source_currency=resolved.source_currency,
            target_amount_minor=resolved.target_amount_minor,
            target_currency=resolved.target_currency,
            rate=resolved.rate,
            purpose=resolved.purpose,
            source=resolved.source,
            path=resolved.path,
            observed_at=resolved.observed_at,
            proxy=resolved.proxy,
            proxy_asset=resolved.proxy_asset,
            expires_at=resolved.expires_at,
            stale=resolved.stale,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "source_amount_minor": str(self.source_amount_minor),
            "source_currency": self.source_currency,
            "target_amount_minor": str(self.target_amount_minor),
            "target_currency": self.target_currency,
            "rate": str(self.rate),
            "purpose": self.purpose.value,
            "source": self.source,
            "path": self.path,
            "observed_at": self.observed_at.isoformat(),
            "proxy": "true" if self.proxy else "false",
            "proxy_asset": self.proxy_asset,
            "expires_at": self.expires_at.isoformat() if self.expires_at else "",
            "stale": "true" if self.stale else "false",
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> ConversionSnapshot:
        try:
            source_amount = data["source_amount_minor"]
            target_amount = data["target_amount_minor"]
            if isinstance(source_amount, bool) or isinstance(target_amount, bool):
                raise ValueError("snapshot amounts cannot be boolean")
            proxy_raw = str(data.get("proxy", "false")).lower()
            if proxy_raw not in {"true", "false"}:
                raise ValueError("snapshot proxy must be true or false")
            stale_raw = str(data.get("stale", "false")).lower()
            if stale_raw not in {"true", "false"}:
                raise ValueError("snapshot stale must be true or false")
            expires_raw = str(data.get("expires_at", "")).strip()
            return cls(
                source_amount_minor=int(source_amount),
                source_currency=str(data["source_currency"]),
                target_amount_minor=int(target_amount),
                target_currency=str(data["target_currency"]),
                rate=parse_rate(data["rate"], field="conversion_snapshot.rate"),
                purpose=FxPurpose(str(data["purpose"])),
                source=str(data["source"]),
                path=str(data.get("path", "")),
                observed_at=datetime.fromisoformat(str(data["observed_at"])),
                proxy=proxy_raw == "true",
                proxy_asset=str(data.get("proxy_asset", "")),
                expires_at=datetime.fromisoformat(expires_raw) if expires_raw else None,
                stale=stale_raw == "true",
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise FxInvalidQuoteError(f"malformed conversion snapshot: {exc}") from exc


__all__ = [
    "CURRENCY_EXPONENTS",
    "GLOBAL_FIAT_CURRENCIES",
    "SUPPORTED_CURRENCIES",
    "ConversionSnapshot",
    "FxError",
    "FxInvalidQuoteError",
    "FxMarketQuote",
    "FxProxyNotAllowedError",
    "FxPurpose",
    "FxReferenceQuote",
    "FxStaleError",
    "FxUnavailableError",
    "FxUnsupportedCurrencyError",
    "ResolvedMoney",
    "currency_exponent",
    "major_to_minor",
    "minor_to_major",
    "normalize_currency",
    "parse_rate",
]
