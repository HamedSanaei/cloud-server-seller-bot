"""Networking domain values (M13-007 rDNS).

Acceptance: IP ownership and validation.

Every PTR target is validated against RFC-1035 hostname rules and every
IP is parsed strictly with :mod:`ipaddress` - no string slop reaches the
provider. Ownership is proven at the service layer: an IP may only be
pointed when the provider reports it as one of THAT server's addresses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address

_LABEL = re.compile(r"^(?!-)[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?<!-)$", re.IGNORECASE)
_MAX_NAME_LENGTH = 253


class RdnsError(Exception):
    """Base class for reverse-DNS command errors."""


class RdnsValidationError(RdnsError, ValueError):
    """Malformed IP or PTR hostname."""


class RdnsNotOwnerError(RdnsError):
    """The server does not belong to the caller (reported as not found)."""


class RdnsUnsupportedError(RdnsError):
    """The provider does not implement reverse DNS management."""


class RdnsIpNotAssignedError(RdnsError):
    """The IP is not assigned to this server (ownership proof failed)."""


def validate_ip(ip: str) -> str:
    """Parse ``ip`` strictly; returns the canonical textual form."""
    try:
        return str(ip_address((ip or "").strip()))
    except ValueError as exc:
        raise RdnsValidationError(f"invalid IP address: {ip!r}") from exc


def validate_ptr(ptr: str) -> str:
    """Validate a PTR hostname (RFC-1035 labels, <= 253 chars total).

    A single trailing dot (root) is accepted and stripped to canonical
    form. Empty/None means "reset to automatic" and is handled by the
    service, not here.
    """
    name = (ptr or "").strip().rstrip(".").lower()
    if not name:
        raise RdnsValidationError("PTR hostname must not be empty")
    if len(name) > _MAX_NAME_LENGTH:
        raise RdnsValidationError(f"PTR hostname longer than {_MAX_NAME_LENGTH} chars")
    labels = name.split(".")
    if len(labels) < 2:
        raise RdnsValidationError("PTR hostname needs at least two labels")
    for label in labels:
        if not _LABEL.match(label):
            raise RdnsValidationError(f"invalid PTR label: {label!r}")
    return name


@dataclass(frozen=True, slots=True)
class ReverseDnsRecord:
    """One validated (IP -> PTR) assignment request."""

    ip: str
    ptr: str | None  # None = reset to the provider's automatic record

    def __post_init__(self) -> None:
        object.__setattr__(self, "ip", validate_ip(self.ip))
        if self.ptr is not None:
            object.__setattr__(self, "ptr", validate_ptr(self.ptr))
