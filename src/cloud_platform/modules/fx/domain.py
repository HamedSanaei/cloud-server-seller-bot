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
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
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


#: Minor-unit exponents (decimals) per currency. Zero-decimal currencies
#: store the display unit directly (IRT minor == Toman, IRR minor == Rial).
CURRENCY_EXPONENTS: dict[str, int] = {
    "IRT": 0,
    "IRR": 0,
    "EUR": 2,
    "USD": 2,
    "JPY": 0,
    "KRW": 0,
}

#: Currencies the resolver explicitly supports (routing is defined for these).
SUPPORTED_CURRENCIES: frozenset[str] = frozenset({"IRT", "IRR", "EUR", "USD"})


def normalize_currency(currency: str) -> str:
    """Upper-case asset code or raise.

    Wallet/c settlement currencies are 3-letter ISO codes; FX market base
    assets include 4-letter codes (USDT as the explicit USD proxy). Both
    pass through here; the resolver enforces which pairs are convertible.
    """
    code = (currency or "").strip().upper()
    if not (2 <= len(code) <= 5) or not code.isalpha():
        raise FxUnsupportedCurrencyError(f"unsupported currency {currency!r}")
    return code


def currency_exponent(currency: str) -> int:
    """Decimals of ``currency`` (0 for IRT/IRR, 2 for EUR/USD)."""
    return CURRENCY_EXPONENTS.get(normalize_currency(currency), 2)


def minor_to_major(amount_minor: int, currency: str) -> Decimal:
    """Integer minor units -> major Decimal (no float)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValueError("amount_minor must be an integer")
    exp = currency_exponent(currency)
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
    quantized = amount_major.quantize(quantum, rounding=_rounding_for(purpose))
    return int(quantized * (Decimal(10) ** exp))


def parse_rate(raw: object, *, field: str) -> Decimal:
    """Parse a provider rate string into a positive Decimal (never float)."""
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise FxInvalidQuoteError(f"malformed rate field {field!r}") from exc
    if value <= 0:
        raise FxInvalidQuoteError(f"non-positive rate field {field!r}")
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
        if not isinstance(self.buy_rate, Decimal) or self.buy_rate <= 0:
            raise FxInvalidQuoteError("buy_rate must be a positive Decimal")
        if not isinstance(self.sell_rate, Decimal) or self.sell_rate <= 0:
            raise FxInvalidQuoteError("sell_rate must be a positive Decimal")
        if not self.source or not self.source.strip():
            raise FxInvalidQuoteError("source must not be empty")
        if not self.source_market or not self.source_market.strip():
            raise FxInvalidQuoteError("source_market must not be empty")


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
        if isinstance(self.source_amount_minor, bool) or self.source_amount_minor < 0:
            raise ValueError("source_amount_minor must be a non-negative int")
        if isinstance(self.target_amount_minor, bool) or self.target_amount_minor < 0:
            raise ValueError("target_amount_minor must be a non-negative int")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_currency", normalize_currency(self.source_currency))
        object.__setattr__(self, "target_currency", normalize_currency(self.target_currency))

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
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> ConversionSnapshot:
        try:
            return cls(
                source_amount_minor=int(data["source_amount_minor"]),
                source_currency=str(data["source_currency"]),
                target_amount_minor=int(data["target_amount_minor"]),
                target_currency=str(data["target_currency"]),
                rate=Decimal(str(data["rate"])),
                purpose=FxPurpose(str(data["purpose"])),
                source=str(data["source"]),
                path=str(data.get("path", "")),
                observed_at=datetime.fromisoformat(str(data["observed_at"])),
                proxy=str(data.get("proxy", "false")).lower() == "true",
                proxy_asset=str(data.get("proxy_asset", "")),
            )
        except (KeyError, ValueError, InvalidOperation) as exc:
            raise FxInvalidQuoteError(f"malformed conversion snapshot: {exc}") from exc


__all__ = [
    "CURRENCY_EXPONENTS",
    "SUPPORTED_CURRENCIES",
    "ConversionSnapshot",
    "FxError",
    "FxInvalidQuoteError",
    "FxMarketQuote",
    "FxProxyNotAllowedError",
    "FxPurpose",
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
