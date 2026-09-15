"""Provider-neutral currency / FX resolution subsystem."""

from cloud_platform.modules.fx.cache import InMemoryFxCache, RedisFxCache, build_fx_cache
from cloud_platform.modules.fx.domain import (
    ConversionSnapshot,
    FxError,
    FxInvalidQuoteError,
    FxMarketQuote,
    FxProxyNotAllowedError,
    FxPurpose,
    FxStaleError,
    FxUnavailableError,
    FxUnsupportedCurrencyError,
    ResolvedMoney,
    currency_exponent,
    major_to_minor,
    minor_to_major,
    normalize_currency,
)
from cloud_platform.modules.fx.formatting import format_minor, format_minor_signed
from cloud_platform.modules.fx.ports import FxCache, FxRateSource, fx_cache_key
from cloud_platform.modules.fx.service import FxConfig, FxResolver

__all__ = [
    "ConversionSnapshot",
    "FxCache",
    "FxConfig",
    "FxError",
    "FxInvalidQuoteError",
    "FxMarketQuote",
    "FxProxyNotAllowedError",
    "FxPurpose",
    "FxRateSource",
    "FxResolver",
    "FxStaleError",
    "FxUnavailableError",
    "FxUnsupportedCurrencyError",
    "InMemoryFxCache",
    "RedisFxCache",
    "ResolvedMoney",
    "build_fx_cache",
    "currency_exponent",
    "format_minor",
    "format_minor_signed",
    "fx_cache_key",
    "major_to_minor",
    "minor_to_major",
    "normalize_currency",
]
