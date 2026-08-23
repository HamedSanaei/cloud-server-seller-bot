"""HMAC helpers for verifying signed webhook callbacks from gateways.

Gateways sign the raw request body with a per-gateway secret using
HMAC-SHA256 (hex). Verification uses constant-time comparison to prevent
timing attacks; malformed or non-ASCII signatures simply fail.
"""

from __future__ import annotations

import hashlib
import hmac


def sign_gateway_payload(secret: str, body: bytes) -> str:
    """Return the hex HMAC-SHA256 signature of ``body`` under ``secret``."""
    if not secret:
        raise ValueError("secret must not be empty")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_gateway_signature(secret: str, body: bytes, signature: str) -> bool:
    """Verify ``signature`` against ``body`` using constant-time comparison."""
    if not secret or not signature:
        return False
    candidate = signature.strip().lower()
    if not candidate or not candidate.isascii():
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, candidate)
