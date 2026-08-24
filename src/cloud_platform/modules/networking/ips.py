"""IP address domain values (M13-008).

Floating (primary) IPs are independent billable resources owned by a
user, optionally bound to one of their servers. The IP string is parsed
strictly; the kind distinguishes provider primary addresses from
floating reservations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from ipaddress import ip_address
from uuid import UUID


class IpAddressError(Exception):
    """Base class for IP resource errors."""


class IpNotFoundError(IpAddressError):
    """Another user's (or unknown) IP reads as missing."""


class IpLimitError(IpAddressError):
    """The user hit their floating-IP quota."""


class IpBoundError(IpAddressError):
    """Releasing or re-binding an IP that is still assigned to a server."""


class IpKind(StrEnum):
    PRIMARY = "primary"
    FLOATING = "floating"


@dataclass(frozen=True, slots=True)
class IpAddress:
    """One provider-assigned address owned by a platform user."""

    user_id: UUID
    provider_account_id: UUID
    provider_key: str
    provider_ip_id: str
    ip: str
    kind: IpKind = IpKind.FLOATING
    location_id: str | None = None
    server_id: UUID | None = None  # current binding, if any
    id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "ip", str(ip_address((self.ip or "").strip())))
        except ValueError as exc:
            raise IpAddressError(f"invalid IP address: {self.ip!r}") from exc

    @property
    def is_bound(self) -> bool:
        return self.server_id is not None
