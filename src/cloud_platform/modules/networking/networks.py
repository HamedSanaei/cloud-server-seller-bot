"""Private network domain values (M13-010).

Acceptance: network lifecycle and capabilities.

A private network is a user-owned L2 segment with one IPv4 range;
servers join and leave it through the provider's optional ``networks``
port. The IP range is parsed strictly with :mod:`ipaddress` - private
ranges only, per RFC-1918.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Network, ip_network
from uuid import UUID

_MAX_NAME_LENGTH = 64


class NetworkError(Exception):
    """Base class for private-network errors."""


class NetworkNotFoundError(NetworkError):
    """Another user's (or unknown) network reads as missing."""


class NetworkLimitError(NetworkError):
    """The user hit their network quota."""


class NetworkRangeError(NetworkError, ValueError):
    """Invalid or non-private IP range."""


@dataclass(frozen=True, slots=True)
class Network:
    """One user-owned private network."""

    user_id: UUID
    provider_account_id: UUID
    provider_key: str
    provider_network_id: str
    name: str
    ip_range: str  # canonical CIDR, e.g. 10.0.0.0/16
    server_ids: tuple[UUID, ...] = ()  # currently joined platform servers
    location_id: str | None = None
    id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise NetworkError("network name must not be empty")
        if len(self.name) > _MAX_NAME_LENGTH:
            raise NetworkError(f"network name longer than {_MAX_NAME_LENGTH} chars")
        object.__setattr__(self, "name", self.name.strip())
        try:
            net = ip_network((self.ip_range or "").strip(), strict=False)
        except ValueError as exc:
            raise NetworkRangeError(f"invalid IP range: {self.ip_range!r}") from exc
        if not isinstance(net, IPv4Network):
            raise NetworkRangeError("only IPv4 ranges are supported for private networks")
        if not net.is_private:
            raise NetworkRangeError(f"{self.ip_range} is not a private (RFC-1918) range")
        object.__setattr__(self, "ip_range", str(net))

    def joined(self, server_id: UUID) -> bool:
        return server_id in self.server_ids
