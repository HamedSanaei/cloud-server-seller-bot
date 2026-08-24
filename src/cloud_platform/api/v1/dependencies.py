"""v1 request dependencies: identity, tokens and scopes (M14-001/M14-002).

Two authentication paths, in priority order:

1. **Bearer API token** (``Authorization: Bearer cpt_...``) - resolved
   through the app-state ``authenticate_token`` callable bound to the
   platform :class:`TokenService` at startup. Revocable, scoped.
2. **Header identity** (``x-platform-user``) - a DEV/TEST fallback that is
   DISABLED unless ``settings.api_allow_header_identity`` is true.

Scope enforcement is declarative per endpoint via :func:`require_scope`;
a token without the scope gets the stable ``403 forbidden`` envelope.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request

from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope

from .errors import ApiError, ErrorCode

#: The dev/test identity header (never valid in production mode).
USER_HEADER = "x-platform-user"
IDEMPOTENCY_HEADER = "idempotency-key"
BEARER_PREFIX = "Bearer cpt_"

MAX_IDEMPOTENCY_KEY_LEN = 128


async def get_current_user_id(x_platform_user: str | None = Header(default=None)) -> UUID:
    """Resolve the acting user; a missing/invalid identity is unauthorized."""
    if not x_platform_user:
        raise ApiError(ErrorCode.UNAUTHORIZED, "missing identity header")
    try:
        return UUID(x_platform_user)
    except ValueError as exc:
        raise ApiError(ErrorCode.UNAUTHORIZED, "identity is not a valid user id") from exc


async def get_token_authentication(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> TokenAuthentication:
    """Authenticate the request via bearer token or (if enabled) dev header."""
    if authorization and authorization.startswith(BEARER_PREFIX):
        authenticator = getattr(request.app.state, "authenticate_token", None)
        if authenticator is None:
            raise ApiError(ErrorCode.UNAUTHORIZED, "token authentication unavailable")
        auth = await authenticator(authorization.removeprefix("Bearer ").strip())
        return auth  # type: ignore[no-any-return]

    # dev fallback - explicitly gated by settings; off in production
    from cloud_platform.core.config import get_settings

    if get_settings().api_allow_header_identity:
        user_id = await get_current_user_id(request.headers.get(USER_HEADER))
        return TokenAuthentication(
            user_id=user_id,
            scopes=frozenset(TokenScope),
            token_id=user_id,  # dev identities have no token record
        )
    raise ApiError(ErrorCode.UNAUTHORIZED, "missing credentials")


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


def require_scope(
    scope: TokenScope,
) -> Callable[[TokenAuthentication], Awaitable[TokenAuthentication]]:
    """Dependency factory enforcing one token scope (403 when lacking)."""

    async def _checker(
        auth: Annotated[TokenAuthentication, Depends(get_token_authentication)],
    ) -> TokenAuthentication:
        if not auth.has(scope):
            raise ApiError(ErrorCode.FORBIDDEN, f"token lacks required scope {scope.value!r}")
        return auth

    return _checker
