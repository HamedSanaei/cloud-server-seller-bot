"""NOWPayments native IPN signature verification (HMAC-SHA512).

NOWPayments signs its native Instant Payment Notification callbacks with the
``x-nowpayments-sig`` header: the provider HMAC-SHA512s a *canonical* rendering
of the JSON body (object keys sorted recursively, arrays kept in their original
order, compact separators) keyed by the merchant's IPN secret.

This module is deliberately tiny and pure: it proves that a payload was signed
with the shared secret and hands back the parsed data. It NEVER credits a
wallet, mutates state or derives authorization from any payload field —
``user_id``/``amount``/``payment_status`` inside an IPN are data, not proof of
entitlement, and the caller must reconcile them against its own durable facts.

Only HMAC-SHA512 is accepted (the algorithm NOWPayments documents for the
native IPN); the platform must never silently fall back to HMAC-SHA256.

Signing uses JavaScript JSON.parse/JSON.stringify number formatting. Exact
Decimal values are materialized separately for financial validation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from decimal import Decimal
from typing import Any, cast

__all__ = ["SignatureError", "canonical_json", "verify_ipn_signature"]

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_SHA512_HEX_LENGTH = 128


class SignatureError(ValueError):
    """The IPN body or signature is malformed, or the signature does not match."""


class _RawNumber:
    """A JSON number preserved as its exact source token (never a float)."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text


def _reject_constant(token: str) -> Any:
    raise SignatureError(f"non-finite JSON number {token!r} is not allowed")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SignatureError(f"duplicate JSON key {key!r} is not allowed")
        result[key] = value
    return result


def _parse_body(raw_body: bytes | bytearray | memoryview | str) -> Any:
    if isinstance(raw_body, (bytes, bytearray, memoryview)):
        try:
            text = bytes(raw_body).decode("utf-8")
        except UnicodeDecodeError:
            raise SignatureError("IPN body is not valid UTF-8") from None
    elif isinstance(raw_body, str):
        text = raw_body
    else:
        raise SignatureError("IPN body must be bytes or text")
    try:
        return json.loads(
            text,
            parse_float=_RawNumber,
            parse_int=_RawNumber,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_without_duplicates,
        )
    except SignatureError:
        raise
    except (ValueError, RecursionError):
        raise SignatureError("IPN body is not valid JSON") from None


def _canonical(value: Any) -> str:
    if isinstance(value, _RawNumber):
        number = float(value.text)
        if not math.isfinite(number):
            raise SignatureError("IPN number is outside the finite signing range")
        if number == 0:
            return "0"
        # Python and ECMAScript use shortest round-trippable binary64 digits;
        # ECMAScript switches to exponent notation outside [1e-6, 1e21).
        shortest = repr(number).lower()
        mantissa, _, exponent_text = shortest.partition("e")
        exponent = int(exponent_text or "0")
        if 1e-6 <= abs(number) < 1e21:
            plain = format(Decimal(shortest), "f")
            return plain.rstrip("0").rstrip(".") if "." in plain else plain
        mantissa = mantissa.removesuffix(".0")
        return f"{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{_string(key)}:{_canonical(value[key])}" for key in sorted(value, key=_key_order)
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    raise SignatureError(f"unsupported JSON value {type(value).__name__}")


def _string(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SignatureError("IPN contains malformed Unicode") from None
    return json.dumps(value, ensure_ascii=False)


def _key_order(key: str) -> tuple[int, Any]:
    # JS enumerates array-index keys numerically even after Object.keys.sort().
    if (
        len(key) <= 10
        and key.isascii()
        and key.isdigit()
        and str(int(key)) == key
        and int(key) < 2**32 - 1
    ):
        return (0, int(key))
    return (1, key.encode("utf-16-be", errors="surrogatepass"))


def _materialize(value: Any) -> Any:
    if isinstance(value, _RawNumber):
        text = value.text
        if "." in text or "e" in text or "E" in text:
            return Decimal(text)
        return int(text)
    if isinstance(value, dict):
        return {key: _materialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_materialize(item) for item in value]
    return value


def canonical_json(raw_body: bytes | bytearray | memoryview | str) -> str:
    """Return the compact canonical JSON of a raw IPN body.

    Object keys are sorted recursively, arrays keep their order and numbers are
    formatted as JavaScript numbers. Raises :class:`SignatureError` for
    any body that is not a single JSON object.
    """
    parsed = _parse_body(raw_body)
    if not isinstance(parsed, dict) or not parsed:
        raise SignatureError("IPN body must be a non-empty JSON object")
    return _canonical(parsed)


def verify_ipn_signature(
    raw_body: bytes | bytearray | memoryview | str,
    signature: str,
    ipn_secret: str,
) -> dict[str, Any]:
    """Verify ``x-nowpayments-sig`` and return the parsed payload.

    The body must be the EXACT bytes received (re-encoding a parsed object can
    change number formatting and invalidate the signature). Comparison is
    constant-time. Raises :class:`SignatureError` when the body is malformed or
    the signature does not match; on success returns the parsed object with
    numbers materialized as ``int``/``Decimal``.
    """
    if not isinstance(ipn_secret, str) or not ipn_secret:
        raise SignatureError("IPN secret is required")
    if not isinstance(signature, str):
        raise SignatureError("signature is required")
    candidate = signature.strip()
    if len(candidate) != _SHA512_HEX_LENGTH or not all(char in _HEX_DIGITS for char in candidate):
        raise SignatureError("signature is malformed")
    parsed = _parse_body(raw_body)
    if not isinstance(parsed, dict) or not parsed:
        raise SignatureError("IPN body must be a non-empty JSON object")
    try:
        expected = hmac.new(
            ipn_secret.encode("utf-8"),
            _canonical(parsed).encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
    except (UnicodeEncodeError, RecursionError):
        raise SignatureError("IPN contains invalid signing data") from None
    if not hmac.compare_digest(expected, candidate.lower()):
        raise SignatureError("IPN signature does not match")
    return cast(dict[str, Any], _materialize(parsed))
