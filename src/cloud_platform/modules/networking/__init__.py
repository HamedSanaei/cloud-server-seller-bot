"""Networking module (M13-007): reverse DNS management."""

from .domain import (
    RdnsError,
    RdnsIpNotAssignedError,
    RdnsNotOwnerError,
    RdnsUnsupportedError,
    RdnsValidationError,
    ReverseDnsRecord,
    validate_ip,
    validate_ptr,
)
from .service import RdnsService

__all__ = [
    "RdnsError",
    "RdnsIpNotAssignedError",
    "RdnsNotOwnerError",
    "RdnsService",
    "RdnsUnsupportedError",
    "RdnsValidationError",
    "ReverseDnsRecord",
    "validate_ip",
    "validate_ptr",
]
