"""REST API v1 (M14-001).

Acceptance: stable resource/error/idempotency contract.

The v1 surface is CONTRACT-FROZEN:

- **Errors** always use one envelope -
  ``{"error": {"code": "<stable-code>", "message": str, "details": object|null}}``
  - with codes from :class:`ErrorCode`. Domain exceptions map to stable
  codes in :data:`DOMAIN_ERROR_MAP`; ownership violations deliberately map
  to ``not_found`` (no existence leak).
- **Idempotency**: every mutating endpoint REQUIRES an ``Idempotency-Key``
  header (428 + ``idempotency_required`` otherwise; >128 chars is a 400).
  The key feeds the platform's ledger operations, so a replayed request
  can never double-act.
- **Resources** are versioned under ``/v1`` and documented in
  ``docs/api/REST_API_V1.md``. The contract test suite pins the paths and
  the envelope shape.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from cloud_platform.modules.firewalls.domain import FirewallNotFoundError
from cloud_platform.modules.sshkeys.domain import (
    DuplicateSshKeyError,
    InvalidPublicKeyError,
    SshKeyLimitError,
    SshKeyNotFoundError,
)


class ErrorCode(StrEnum):
    """Stable machine-readable error codes (v1 contract - never rename)."""

    VALIDATION_ERROR = "validation_error"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    QUOTA_EXCEEDED = "quota_exceeded"
    INVALID_SSH_KEY = "invalid_ssh_key"
    ACTION_NOT_ALLOWED = "action_not_allowed"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    IDEMPOTENCY_REQUIRED = "idempotency_required"
    RATE_LIMITED = "rate_limited"
    NOT_IMPLEMENTED = "not_implemented"
    INTERNAL_ERROR = "internal_error"


STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.QUOTA_EXCEEDED: 409,
    ErrorCode.INVALID_SSH_KEY: 400,
    ErrorCode.ACTION_NOT_ALLOWED: 409,
    ErrorCode.OPERATION_IN_PROGRESS: 409,
    ErrorCode.IDEMPOTENCY_REQUIRED: 428,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.NOT_IMPLEMENTED: 501,
    ErrorCode.INTERNAL_ERROR: 500,
}


def error_response(code: ErrorCode, message: str, details: object | None = None) -> JSONResponse:
    """The ONE error envelope for every v1 failure."""
    return JSONResponse(
        status_code=STATUS_BY_CODE[code],
        content={"error": {"code": code.value, "message": message, "details": details}},
    )


class ApiError(Exception):
    """Raise anywhere in a v1 handler to emit the stable envelope."""

    def __init__(self, code: ErrorCode, message: str, details: object | None = None) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


#: Domain exception -> stable code. Ownership errors map to not_found so
#: foreign resources are indistinguishable from missing ones.
DOMAIN_ERROR_MAP: tuple[tuple[type[Exception], ErrorCode], ...] = (
    (SshKeyNotFoundError, ErrorCode.NOT_FOUND),
    (FirewallNotFoundError, ErrorCode.NOT_FOUND),
    (DuplicateSshKeyError, ErrorCode.CONFLICT),
    (SshKeyLimitError, ErrorCode.QUOTA_EXCEEDED),
    (InvalidPublicKeyError, ErrorCode.INVALID_SSH_KEY),
)


def install_error_handlers(app: FastAPI) -> None:
    """Register the v1 envelope handlers on the app."""

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.code, exc.message, exc.details)

    for exc_type, code in DOMAIN_ERROR_MAP:

        def _domain_handler(
            request: Request, exc: Exception, _code: ErrorCode = code
        ) -> JSONResponse:
            return error_response(_code, str(exc))

        app.add_exception_handler(exc_type, _domain_handler)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            ErrorCode.VALIDATION_ERROR, "request validation failed", {"errors": exc.errors()}
        )

    #: Plain HTTP exceptions (framework 404s etc.) also get the envelope.
    CODE_BY_HTTP_STATUS: dict[int, ErrorCode] = {
        400: ErrorCode.VALIDATION_ERROR,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.UNAUTHORIZED,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        429: ErrorCode.RATE_LIMITED,
        501: ErrorCode.NOT_IMPLEMENTED,
    }

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = CODE_BY_HTTP_STATUS.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return error_response(code, str(exc.detail))
