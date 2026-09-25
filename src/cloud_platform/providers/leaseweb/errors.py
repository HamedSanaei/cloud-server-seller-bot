"""Leaseweb error model (LEASEWEB-VPS-API).

A stable exception hierarchy for the whole Leaseweb integration instead of
leaking ``httpx`` exceptions into application code. Every class derives from
:class:`~cloud_platform.providers.errors.ProviderError` so the platform's
existing retry classification, operation ledger and reconciliation logic
keep working unchanged:

- ``LeasewebAuthenticationError`` / ``LeasewebForbiddenError`` are
  ``ProviderAuthError`` (permanent).
- ``LeasewebNotFoundError`` is ``ProviderNotFound`` (permanent).
- ``LeasewebConflictError`` is ``ProviderConflict`` (permanent).
- ``LeasewebValidationError`` is a plain ``ProviderError`` (permanent).
- ``LeasewebRateLimitError`` is ``ProviderRateLimited`` (read-only retryable).
- ``LeasewebServerError`` / ``LeasewebUnavailableError`` /
  ``LeasewebTimeoutError`` are ``ProviderUnavailable`` (retryable).
- ``LeasewebAmbiguousMutationError`` is ``ProviderOutcomeUnknown``: a
  mutation may or may not have been applied and must NEVER be blindly
  re-sent.

Security: an error carries only *safe* provider facts (HTTP status,
``correlationId``, ``errorCode``, ``reference``, a redacted message). It
never carries the API key, request credentials, passwords, SSH keys or
console secrets. Provider payloads are scrubbed by :func:`redact_sensitive`
before they can reach a message, a log record or an exception repr.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderCapacityError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderUnavailable,
)

__all__ = [
    "CAPACITY_ERROR_CODES",
    "CORRELATION_HEADER",
    "LeasewebAmbiguousMutationError",
    "LeasewebAuthenticationError",
    "LeasewebCapacityError",
    "LeasewebConflictError",
    "LeasewebError",
    "LeasewebErrorPayload",
    "LeasewebForbiddenError",
    "LeasewebNotFoundError",
    "LeasewebRateLimitError",
    "LeasewebResponseError",
    "LeasewebServerError",
    "LeasewebTimeoutError",
    "LeasewebUnavailableError",
    "LeasewebValidationError",
    "is_capacity_exhausted",
    "parse_error_payload",
    "redact_sensitive",
]

#: Response header Leaseweb support can use to trace one API request. It is
#: a routing identifier, never a credential, so it is safe to log and to
#: quote to Leaseweb support.
CORRELATION_HEADER = "APIGW-CORRELATION-ID"

#: Leaseweb error codes that mean "this credential account cannot accept a NEW
#: billable instance right now". Observed live on ``POST /publicCloud/v1/instances``
#: for the Frankfurt Sales Organization while it already ran two instances::
#:
#:     {"errorCode": "PC-2031", "errorMessage": "Customer limit reached"}
#:
#: It is a CAPACITY fact about the account, not a defect of the requested
#: offer, and retrying the same account does not clear it.
CAPACITY_ERROR_CODES: frozenset[str] = frozenset({"PC-2031"})

#: Message fragments (case-insensitive) identifying the same condition when a
#: response omits ``errorCode``. Deliberately narrow: a loose "limit" match
#: would misclassify unrelated validation failures as capacity problems.
_CAPACITY_MESSAGE_FRAGMENTS: tuple[str, ...] = (
    "customer limit reached",
    "customer limit has been reached",
    "account limit reached",
)

_MAX_MESSAGE = 300


def is_capacity_exhausted(error_code: str | None, message: str | None) -> bool:
    """Whether a provider failure is an ACCOUNT CAPACITY limit (PC-2031).

    Safe for any input: an unknown/absent code falls back to the documented
    message text, and anything unrecognized is NOT a capacity condition.
    """
    code = (error_code or "").strip().upper()
    if code and code in CAPACITY_ERROR_CODES:
        return True
    text = (message or "").strip().casefold()
    if not text:
        return False
    return any(fragment in text for fragment in _CAPACITY_MESSAGE_FRAGMENTS)


#: Patterns that must never survive into an exception message, a log record
#: or a test snapshot. Provider error payloads may echo request bodies (for
#: example a credential POST), so scrubbing happens at the boundary.
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(x-lsw-auth|authorization)\s*[:=]\s*(?:bearer|basic|token)?\s*\S+"),
        r"\1: <redacted>",
    ),
    (
        re.compile(r'(?i)"(password|newPassword|privateKey|secret|token|apiKey)"\s*:\s*"[^"]*"'),
        r'"\1": "<redacted>"',
    ),
    (re.compile(r"(?i)\b(password|passwd)\s*=\s*\S+"), r"\1=<redacted>"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<redacted-private-key>",
    ),
    (
        # The whole key line is replaced: the algorithm prefix is harmless on
        # its own, but masking the entire match keeps a key material blob from
        # ever surviving next to its context (and is what a redaction test can
        # assert on without pattern-specific knowledge).
        re.compile(r"\b(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-[a-z0-9-]+)\s+[A-Za-z0-9+/=]{16,}"),
        "<redacted-ssh-key>",
    ),
)


def redact_known(text: str, secrets: Iterable[str]) -> str:
    """Scrub KNOWN secret values (API key, credential) out of ``text``.

    Defence in depth for a provider that echoes a value we sent: the value is
    removed even when it does not match a generic pattern. Values shorter
    than 6 characters are ignored so nothing over-redacts.
    """
    redacted = text or ""
    for secret in secrets:
        if secret and len(secret) >= 6 and secret in redacted:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def redact_sensitive(text: str) -> str:
    """Scrub credentials/secrets out of provider-supplied text.

    Applied to every provider message before it becomes part of an
    exception, a log record or a metric label. Best-effort by design: the
    platform additionally never *puts* a secret into an error in the first
    place.
    """
    if not text:
        return ""
    redacted = text
    for pattern, replacement in _REDACTION_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


@dataclass(frozen=True, slots=True)
class LeasewebErrorPayload:
    """The safe subset of a Leaseweb error response."""

    http_status: int
    correlation_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    user_message: str | None = None
    reference: str | None = None
    error_details: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def details_summary(self) -> str:
        """Bounded ``field: reason`` text for ``errorDetails`` (safe for logs)."""
        parts: list[str] = []
        for field_name, reasons in self.error_details.items():
            joined = " | ".join(reason for reason in reasons if reason)
            if joined:
                parts.append(f"{field_name}: {joined}")
        return "; ".join(parts)

    @property
    def summary(self) -> str:
        """A bounded, redacted one-line description (safe for logs).

        Per-field ``errorDetails`` are included: a provider validation failure
        is only actionable when the rejected field and its reason survive. A
        summary that drops them (as production observed for the 400 on
        ``/instances``) leaves an operator with "Validation Failed" and
        nothing to fix. Every string here is already redacted/scrubbed and the
        whole line is bounded.
        """
        parts: list[str] = []
        if self.error_code:
            parts.append(f"errorCode={self.error_code}")
        message = self.error_message or self.user_message
        if message:
            parts.append(message)
        details = self.details_summary
        if details:
            parts.append(details)
        if self.correlation_id:
            parts.append(f"correlationId={self.correlation_id}")
        parts.append(f"HTTP {self.http_status}")
        return redact_sensitive("; ".join(parts))[:_MAX_MESSAGE]


def _as_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _parse_error_details(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {}
    details: dict[str, tuple[str, ...]] = {}
    for key, raw in value.items():
        if isinstance(raw, list):
            items = tuple(redact_sensitive(str(item))[:200] for item in raw if item is not None)
        elif raw is None:
            items = ()
        else:
            items = (redact_sensitive(str(raw))[:200],)
        if items:
            details[str(key)] = items
    return details


def _nested_error_message(payload: dict[str, Any]) -> str | None:
    """Legacy ``Response.Errors.Error`` envelope (some older endpoints)."""
    response = payload.get("Response")
    if not isinstance(response, dict):
        return None
    errors = response.get("Errors")
    if not isinstance(errors, dict):
        return None
    error = errors.get("Error")
    if not isinstance(error, dict):
        return None
    code = _as_str(error.get("Code"))
    message = _as_str(error.get("Message"))
    if code and message:
        return f"{code}: {message}"
    return message or code


def parse_error_payload(
    response: httpx.Response,
    *,
    secrets: Iterable[str] = (),
) -> LeasewebErrorPayload:
    """Parse a Leaseweb error response into safe, typed facts.

    Never raises and never returns a raw payload: unparseable bodies degrade
    to a bounded, redacted text excerpt.

    ``secrets`` are the credential values the caller sent. Leaseweb echoes
    request material back in some error bodies (for example
    ``{"errorMessage": "bad key <API-KEY>"}``), so every provider string is
    additionally scrubbed against the KNOWN values — generic patterns alone
    cannot recognize an opaque API key.
    """
    known = tuple(secrets)

    def scrub(text: str) -> str:
        return redact_known(redact_sensitive(text), known)

    correlation = _as_str(response.headers.get(CORRELATION_HEADER))
    try:
        payload: Any = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        body_correlation = _as_str(payload.get("correlationId") or payload.get("correlationID"))
        message = _as_str(payload.get("errorMessage"))
        if message is None:
            message = _nested_error_message(payload)
        if message is None:
            errors = payload.get("errors")
            if isinstance(errors, list):
                for row in errors:
                    text = _as_str(row)
                    if text:
                        message = text
                        break
        details = {
            field: tuple(scrub(value) for value in values)
            for field, values in _parse_error_details(payload.get("errorDetails")).items()
        }
        return LeasewebErrorPayload(
            http_status=response.status_code,
            correlation_id=correlation or body_correlation,
            error_code=_as_str(payload.get("errorCode") or payload.get("errorCodeName")),
            error_message=scrub(message)[:_MAX_MESSAGE] if message else None,
            user_message=scrub(_as_str(payload.get("userMessage")) or "")[:_MAX_MESSAGE] or None,
            reference=_as_str(payload.get("reference")),
            error_details=details,
        )

    fallback = scrub((response.text or "").strip())[:_MAX_MESSAGE]
    return LeasewebErrorPayload(
        http_status=response.status_code,
        correlation_id=correlation,
        error_message=fallback or f"HTTP {response.status_code}",
    )


class LeasewebError(ProviderError):
    """Base class for every Leaseweb API failure.

    Carries only safe provider facts; ``str(error)`` is the redacted
    provider summary and never contains a credential.
    """

    #: Whether the transport may retry the call that produced this error.
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        payload: LeasewebErrorPayload | None = None,
        endpoint: str | None = None,
    ) -> None:
        safe_message = redact_sensitive(message)[:_MAX_MESSAGE]
        super().__init__(safe_message)
        self.payload = payload
        self.endpoint = endpoint

    # -- safe accessors -------------------------------------------------

    @property
    def status(self) -> int | None:
        return self.payload.http_status if self.payload else None

    @property
    def correlation_id(self) -> str | None:
        return self.payload.correlation_id if self.payload else None

    @property
    def error_code(self) -> str | None:
        return self.payload.error_code if self.payload else None

    @property
    def reference(self) -> str | None:
        return self.payload.reference if self.payload else None

    @property
    def user_message(self) -> str | None:
        return self.payload.user_message if self.payload else None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status!r}, "
            f"error_code={self.error_code!r}, correlation_id={self.correlation_id!r}, "
            f"message={str(self)!r})"
        )


class LeasewebAuthenticationError(LeasewebError, ProviderAuthError):
    """401: the API key is missing, unknown or revoked."""


class LeasewebForbiddenError(LeasewebError, ProviderAuthError):
    """403: authenticated, but not entitled to this resource/operation."""


class LeasewebNotFoundError(LeasewebError, ProviderNotFound):
    """404: the referenced resource does not exist (or is not visible)."""


class LeasewebValidationError(LeasewebError):
    """400/422: the request was rejected as invalid. Permanent."""


class LeasewebCapacityError(LeasewebValidationError, ProviderCapacityError):
    """The credential account has no capacity for NEW instances (PC-2031).

    A distinct condition with a distinct operator response: the request was
    well-formed and the offer is sellable, but this Sales Organization already
    holds its maximum number of instances (``errorCode=PC-2031``,
    ``errorMessage="Customer limit reached"``).

    It is NOT an offer/image problem, so a customer must never be told "this
    offer is unavailable", and it is NOT transient in the retry sense, so the
    platform never re-sends the same POST (least of all through another
    credential account: the accepted contract is pinned to one account).

    The platform records it as a durable per-account capacity signal with a
    TTL, which keeps the storefront from publishing NEW orders through an
    account that has just refused one — while leaving every existing resource
    resolvable through the account it was created with.
    """

    retryable = False

    @property
    def capacity_exhausted(self) -> bool:
        return True


class LeasewebResponseError(LeasewebError):
    """A successful response did not match the documented schema.

    The message carries field names and validation types only — never the
    provider payload, which may contain credentials or console secrets.
    """


class LeasewebConflictError(LeasewebError, ProviderConflict):
    """409/423: the resource is in a state that forbids the operation."""


class LeasewebRateLimitError(LeasewebError, ProviderRateLimited):
    """429: the client is over a Leaseweb rate limit."""

    retryable = True

    def __init__(
        self,
        message: str,
        reset_at_unix: int | None = None,
        *,
        payload: LeasewebErrorPayload | None = None,
        endpoint: str | None = None,
    ) -> None:
        LeasewebError.__init__(self, message, payload=payload, endpoint=endpoint)
        self.reset_at_unix = reset_at_unix


class LeasewebServerError(LeasewebError, ProviderUnavailable):
    """5xx: Leaseweb failed to serve the request."""

    retryable = True


class LeasewebUnavailableError(LeasewebError, ProviderUnavailable):
    """The transport could not reach Leaseweb (retryable)."""

    retryable = True


class LeasewebTimeoutError(LeasewebUnavailableError):
    """A request timed out. Ambiguous for mutations, retryable for reads."""

    retryable = True


class LeasewebAmbiguousMutationError(LeasewebError, ProviderOutcomeUnknown):
    """A mutating request may or may not have been applied.

    Raised when the transport cannot prove whether Leaseweb processed the
    mutation (read/write timeout after transmission, dropped connection,
    mutating 429, 5xx after transmission, unparseable success body). The
    platform records the operation as ``PROVIDER_OUTCOME_UNKNOWN`` and never
    re-sends it automatically.
    """

    retryable = False


#: HTTP status -> error class for the documented error responses.
_STATUS_TO_ERROR: dict[int, type[LeasewebError]] = {
    400: LeasewebValidationError,
    401: LeasewebAuthenticationError,
    403: LeasewebForbiddenError,
    404: LeasewebNotFoundError,
    409: LeasewebConflictError,
    422: LeasewebValidationError,
    423: LeasewebConflictError,
}


def error_for_response(
    payload: LeasewebErrorPayload,
    *,
    endpoint: str | None = None,
) -> LeasewebError:
    """Map a parsed error payload onto the Leaseweb exception hierarchy."""
    status = payload.http_status
    cls: type[LeasewebError]
    if status == 429:
        return LeasewebRateLimitError(payload.summary, payload=payload, endpoint=endpoint)
    if status >= 500:
        return LeasewebServerError(payload.summary, payload=payload, endpoint=endpoint)
    # Capacity/account-limit failures arrive as HTTP 400 with a dedicated
    # provider code. They are classified BEFORE the generic status table so
    # the platform can act on the account, not on the request shape.
    if status in (400, 409, 422) and is_capacity_exhausted(
        payload.error_code, payload.error_message or payload.user_message
    ):
        return LeasewebCapacityError(payload.summary, payload=payload, endpoint=endpoint)
    fallback = LeasewebValidationError if status < 500 else LeasewebServerError
    cls = _STATUS_TO_ERROR.get(status, fallback)
    return cls(payload.summary, payload=payload, endpoint=endpoint)
