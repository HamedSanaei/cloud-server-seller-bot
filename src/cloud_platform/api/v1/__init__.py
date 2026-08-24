"""REST API v1 (M14-001): stable resource/error/idempotency contract."""

from .errors import ApiError, ErrorCode, error_response, install_error_handlers
from .router import router

__all__ = [
    "ApiError",
    "ErrorCode",
    "error_response",
    "install_error_handlers",
    "router",
]
