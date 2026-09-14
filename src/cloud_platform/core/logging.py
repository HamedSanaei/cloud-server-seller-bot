"""Structured logging configuration with sensitive field redaction.

This module configures ``structlog`` to emit JSON-formatted log records and
automatically redacts sensitive fields such as authorization tokens, passwords,
and cloud-init secrets.

Usage::

    from cloud_platform.core.logging import get_logger

    logger = get_logger(__name__)
    logger.info("user authenticated", user_id=123)
    # {"event": "user authenticated", "user_id": 123, "level": "info", ...}
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

import structlog
from structlog.types import EventDict

from cloud_platform.core.config import get_settings

# Fields whose values must always be redacted from log output.
_REDACTED_KEYS: frozenset[str] = frozenset(
    {
        # Authorization and authentication
        "authorization",
        "auth",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "credential",
        # Provider credentials and provider auth headers
        "hetzner_api_token",
        "provider_token",
        "provider_credential_encryption_key",
        "x-lsw-auth",
        "lsw-auth",
        "authorization_header",
        "auth_header",
        # Cloud-init and sensitive data
        "cloud_init",
        "user_data",
        "private_key",
        "ssh_private_key",
        # Internal identifiers
        "database_url",
        "redis_url",
    }
)

# Patterns for redacting values that look like tokens or keys.
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer tokens in Authorization headers
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    # Generic "key=value" / "key: value" patterns containing token-like
    # strings. The separator is preserved so the redacted text stays readable.
    re.compile(r"(?i)\b(password|secret|token|api_key|apikey|credential)(\s*[=:]\s*)\S+"),
)

_REDACTED_PLACEHOLDER = "***REDACTED***"

# Match sensitive terms as whole words (bounded by underscores, dashes, or
# start/end). ``auth`` covers provider auth headers such as ``X-LSW-Auth`` and
# any ``*_auth`` field; exact key names are listed in ``_REDACTED_KEYS``.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(^|[_\-])(token|secret|password|credential|apikey|auth)($|[_\-])", re.IGNORECASE
)


def _should_redact_key(key: str) -> bool:
    """Check if a key name should be redacted.

    Matches exact keys in _REDACTED_KEYS, or keys that contain sensitive
    terms as whole words (e.g., 'api_key', 'access_token' but not 'credentials').
    """
    key_lower = key.lower()
    if key_lower in _REDACTED_KEYS:
        return True
    return bool(_SENSITIVE_KEY_PATTERN.search(key_lower))


def _redact_value(value: object) -> object:
    """Recursively redact sensitive values in dicts and strings."""
    if isinstance(value, dict):
        return {
            str(k): (_REDACTED_PLACEHOLDER if _should_redact_key(str(k)) else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, str):
        redacted = value
        for pattern in _REDACT_PATTERNS:
            redacted = pattern.sub(rf"\1{_REDACTED_PLACEHOLDER}", redacted)
        return redacted
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


#: Maximum length of one string value kept in a sanitized payload. Long
#: blobs (raw HTTP bodies, provider error dumps) are truncated so a business
#: event can never leak a credential inside free text.
_MAX_VALUE_CHARS = 300


def _json_safe(value: object) -> object:
    """Recursively make ``value`` JSON-safe, dropping/limiting unknown types."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:_MAX_VALUE_CHARS]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return str(value.isoformat())[:_MAX_VALUE_CHARS]
    return str(value)[:_MAX_VALUE_CHARS]


def sanitize_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """JSON-safe, redacted, size-bounded copy of an outbound payload.

    Used by the business-log outbox before anything is persisted or sent to
    an operator channel: secrets are replaced by ``***REDACTED***``, values
    are truncated and the whole payload is bounded, so a provider error body
    cannot carry a token into an external chat.
    """
    safe = _json_safe(_redact_value(dict(payload)))
    if not isinstance(safe, dict):  # pragma: no cover - dict input always yields a dict
        return {}
    return {str(k): v for k, v in safe.items() if v is not None and v != ""}


def redact_sensitive_fields(
    _: structlog.types.WrappedLogger,
    __: str,
    event_dict: EventDict,
) -> EventDict:
    """Processor that redacts sensitive fields from log events.

    Redacts values associated with keys like 'token', 'password', 'secret',
    'authorization', etc. Also redacts Bearer token patterns in string values.
    """
    return _redact_value(event_dict)  # type: ignore[return-value]


def _configure_structlog(level: str) -> None:
    """Configure structlog with JSON rendering and redaction processors."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_sensitive_fields,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def _configure_stdlib_logging(level: str) -> None:
    """Configure standard library logging to integrate with structlog."""
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )


def setup_logging() -> None:
    """Set up structured logging with redaction.

    Reads ``LOG_LEVEL`` from application settings and configures both
    ``structlog`` and the standard library ``logging`` module.
    """
    settings = get_settings()
    level = settings.log_level
    _configure_structlog(level)
    _configure_stdlib_logging(level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Logger name, typically ``__name__``. If None, a default logger is returned.

    Returns:
        A configured BoundLogger instance with redaction applied.
    """
    if name is not None:
        return structlog.get_logger(name)  # type: ignore
    return structlog.get_logger()  # type: ignore
