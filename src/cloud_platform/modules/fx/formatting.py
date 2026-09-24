"""Authoritative money presentation (single formatter for the platform).

All customer-facing and admin money text goes through :func:`format_minor`.
The exponent is audited by :mod:`cloud_platform.modules.fx.domain`; in
particular JPY/KRW are never divided by 100. Unknown legacy codes retain the
non-authoritative generic presentation, but they are rejected by FX arithmetic.
"""

from __future__ import annotations


def _grouped(value: int) -> str:
    return f"{value:,}"


def format_minor(amount_minor: int, currency: str, *, allow_legacy: bool = False) -> str:
    """Format integer minor units with the audited currency exponent."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValueError("amount_minor must be an integer (never float)")
    minor = amount_minor
    code = (currency or "").strip().upper()
    if code == "IRT":
        return f"{_grouped(minor)} تومان"
    if code == "IRR":
        return f"{_grouped(minor)} ریال"
    if not code:
        return f"{'-' if minor < 0 else ''}{_grouped(abs(minor) // 100)}.{abs(minor) % 100:02d}"
    try:
        from cloud_platform.modules.fx.domain import (
            FxUnsupportedCurrencyError,
            currency_exponent,
        )

        exponent = currency_exponent(code)
    except FxUnsupportedCurrencyError as exc:
        if not allow_legacy:
            raise ValueError(f"currency {code!r} is not supported for money display") from exc
        # Explicitly opt-in legacy/admin rendering only. Customer-facing
        # callers must never turn an unknown provider code into cents.
        exponent = 2
    if exponent == 0:
        symbols = {"JPY": "¥", "KRW": "₩"}
        symbol = symbols.get(code, "")
        suffix = f" {code}" if code and not symbol else ""
        return f"{'-' if minor < 0 else ''}{symbol}{_grouped(minor)}{suffix}"
    scale = 10**exponent
    sign = "-" if minor < 0 else ""
    major, fraction = divmod(abs(minor), scale)
    fraction_text = f".{fraction:0{exponent}d}" if exponent else ""
    # Keep the long-standing explicit-code presentation for non-USD/global
    # currencies; EUR and USD use their familiar storefront symbols.
    symbols = {"EUR": "€", "USD": "$"}
    symbol = symbols.get(code, "")
    if symbol:
        return (
            f"{symbol}{sign}{major}{fraction_text}"
            if minor >= 0
            else (f"-{symbol}{major}{fraction_text}")
        )
    return f"{sign}{major}{fraction_text} {code}" if code else f"{sign}{major}{fraction_text}"


def format_minor_signed(amount_minor: int, currency: str, *, allow_legacy: bool = False) -> str:
    """Signed ledger-style formatting (``+`` for credits)."""
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise ValueError("amount_minor must be an integer (never float)")
    minor = amount_minor
    code = (currency or "").strip().upper()
    if code in ("IRT", "IRR"):
        sign = "+" if minor >= 0 else "-"
        word = "تومان" if code == "IRT" else "ریال"
        return f"{sign}{_grouped(abs(minor))} {word}"
    base = format_minor(abs(minor), code, allow_legacy=allow_legacy)
    return f"+{base}" if minor >= 0 else f"-{base}"


__all__ = ["format_minor", "format_minor_signed"]
