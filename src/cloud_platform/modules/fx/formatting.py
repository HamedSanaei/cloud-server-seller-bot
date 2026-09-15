"""Authoritative money presentation (single formatter for the platform).

All customer-facing and admin money text goes through :func:`format_minor`.
Rules:

- IRT (Toman): zero-decimal, ``1_250_000`` -> ``"1,250,000 تومان"``. Never
  divided by 100, never shown as IRT code in Persian UI.
- IRR (Rial): zero-decimal, grouped digits + ``"ریال"`` (internal/admin).
- EUR: 2-decimal, ``499`` -> ``"€4.99"``.
- USD: 2-decimal, ``499`` -> ``"$4.99"``.
- Unknown 3-letter codes: generic ``"<major>.<cc> CODE"`` with 2 decimals
  (never crashes a screen; never invents a symbol).

No float is used anywhere; grouping uses integer formatting.
"""

from __future__ import annotations


def _grouped(value: int) -> str:
    return f"{value:,}"


def format_minor(amount_minor: int, currency: str) -> str:
    """Format integer minor units for display (no float)."""
    if isinstance(amount_minor, bool):
        raise ValueError("amount_minor must be an integer")
    minor = int(amount_minor)
    code = (currency or "").strip().upper()
    if code == "IRT":
        return f"{_grouped(minor)} تومان"
    if code == "IRR":
        return f"{_grouped(minor)} ریال"
    if code == "EUR":
        sign = "-" if minor < 0 else ""
        major, cents = divmod(abs(minor), 100)
        return f"{sign}€{major}.{cents:02d}"
    if code == "USD":
        sign = "-" if minor < 0 else ""
        major, cents = divmod(abs(minor), 100)
        return f"${sign}{major}.{cents:02d}" if not sign else f"-${major}.{cents:02d}"
    sign = "-" if minor < 0 else ""
    major, cents = divmod(abs(minor), 100)
    return f"{sign}{major}.{cents:02d} {code}" if code else f"{sign}{major}.{cents:02d}"


def format_minor_signed(amount_minor: int, currency: str) -> str:
    """Signed ledger-style formatting (``+`` for credits)."""
    if isinstance(amount_minor, bool):
        raise ValueError("amount_minor must be an integer")
    minor = int(amount_minor)
    code = (currency or "").strip().upper()
    if code in ("IRT", "IRR"):
        sign = "+" if minor >= 0 else "-"
        word = "تومان" if code == "IRT" else "ریال"
        return f"{sign}{_grouped(abs(minor))} {word}"
    base = format_minor(abs(minor), code)
    return f"+{base}" if minor >= 0 else f"-{base}"


__all__ = ["format_minor", "format_minor_signed"]
