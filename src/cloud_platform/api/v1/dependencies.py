"""v1 request dependencies: user identity + idempotency key (M14-001)."""

from __future__ import annotations

from uuid import UUID

from fastapi import Header

from .errors import ApiError, ErrorCode

#: The auth adapter seam. v1 fixes the CONTRACT (a stable user identity on
#: every request); production binds this dependency to the platform
#: credential system, dev/tests inject values directly.
USER_HEADER = "x-platform-user"
IDEMPOTENCY_HEADER = "idempotency-key"

MAX_IDEMPOTENCY_KEY_LEN = 128


async def get_current_user_id(x_platform_user: str | None = Header(default=None)) -> UUID:
    """Resolve the acting user; a missing/invalid identity is unauthorized."""
    if not x_platform_user:
        raise ApiError(ErrorCode.UNAUTHORIZED, "missing identity header")
    try:
        return UUID(x_platform_user)
    except ValueError as exc:
        raise ApiError(ErrorCode.UNAUTHORIZED, "identity is not a valid user id") from exc


async def require_idempotency_key(
    idempotency_key: str | None = Header(default=None),
) -> str:
    """Every mutating v1 request MUST carry an idempotency key.

    428 ``idempotency_required`` when absent; 400 when oversized. The key
    becomes part of the ledger operation key downstream, so retries of the
    same logical command are safe by construction.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise ApiError(
            ErrorCode.IDEMPOTENCY_REQUIRED,
            f"the {IDEMPOTENCY_HEADER!r} header is required for mutating requests",
        )
    stripped = idempotency_key.strip()
    if len(stripped) > MAX_IDEMPOTENCY_KEY_LEN:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"idempotency key longer than {MAX_IDEMPOTENCY_KEY_LEN} characters",
        )
    return stripped
