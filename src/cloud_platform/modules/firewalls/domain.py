"""Firewall domain (M13-006).

Acceptance: reusable rules and ownership.

A :class:`Firewall` is a NAMED, REUSABLE rulebook owned by exactly ONE
user - it may be applied to any number of that user's servers, and the
same definition never has to be re-entered per server. Rules are
validated first-class values; ownership follows the platform convention
(another user's firewalls are indistinguishable from nonexistent ones).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID


class FirewallError(Exception):
    """Base class for firewall errors."""


class InvalidFirewallRuleError(FirewallError):
    """A rule does not pass structural validation."""


class DuplicateFirewallError(FirewallError):
    """The user already owns a firewall with this name."""


class FirewallNotFoundError(FirewallError):
    """No such firewall FOR THIS USER (foreign rows do not exist here)."""


class FirewallLimitError(FirewallError):
    """The per-user firewall quota is exhausted."""


DIRECTIONS: frozenset[str] = frozenset({"in", "out"})
PROTOCOLS: frozenset[str] = frozenset({"tcp", "udp", "icmp", "gre", "esp"})
MAX_RULES_PER_FIREWALL = 50
MAX_NAME_LENGTH = 64


def _validate_port(port: str | None, protocol: str) -> str | None:
    if port is None or port == "":
        return None  # any port (meaningless for icmp/gre/esp anyway)
    text = port.strip()
    parts = text.split("-", 1)
    try:
        bounds = [int(p) for p in parts]
    except ValueError as exc:
        raise InvalidFirewallRuleError(f"invalid port range {port!r}") from exc
    if len(bounds) > 2 or not all(1 <= b <= 65535 for b in bounds):
        raise InvalidFirewallRuleError(f"invalid port {port!r}")
    if len(bounds) == 2 and bounds[0] > bounds[1]:
        raise InvalidFirewallRuleError(f"inverted port range {port!r}")
    return text


@dataclass(frozen=True, slots=True)
class FirewallRule:
    """One allow-rule; direction selects source vs destination CIDRs."""

    direction: str  # "in" | "out"
    protocol: str  # tcp | udp | icmp | gre | esp
    port: str | None = None  # "80", "80-90"; None = any
    cidrs: tuple[str, ...] = ()  # source_ips (in) / destination_ips (out)

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise InvalidFirewallRuleError(f"invalid direction {self.direction!r}")
        if self.protocol not in PROTOCOLS:
            raise InvalidFirewallRuleError(f"invalid protocol {self.protocol!r}")
        object.__setattr__(self, "port", _validate_port(self.port, self.protocol))
        if not self.cidrs:
            raise InvalidFirewallRuleError("at least one CIDR is required")
        for cidr in self.cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise InvalidFirewallRuleError(f"invalid CIDR {cidr!r}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "protocol": self.protocol,
            "port": self.port,
            "cidrs": list(self.cidrs),
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> FirewallRule:
        return FirewallRule(
            direction=str(raw.get("direction")),
            protocol=str(raw.get("protocol")),
            port=str(raw["port"]) if raw.get("port") else None,
            cidrs=tuple(str(c) for c in (raw.get("cidrs") or [])),
        )


@dataclass(frozen=True, slots=True)
class Firewall:
    """One user-owned reusable rulebook."""

    user_id: UUID
    name: str
    rules: tuple[FirewallRule, ...] = field(default_factory=tuple)
    id: UUID | None = None
    #: Set once the rulebook has been materialized at a provider (sync);
    #: the SAME provider-side firewall is reused on later syncs (by name).
    provider_firewall_id: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        name = (self.name or "").strip()
        object.__setattr__(self, "name", name)
        if not name:
            raise ValueError("firewall name must not be empty")
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"firewall name longer than {MAX_NAME_LENGTH} characters")
        if len(self.rules) > MAX_RULES_PER_FIREWALL:
            raise FirewallLimitError(f"at most {MAX_RULES_PER_FIREWALL} rules per firewall")

    def to_dicts(self) -> list[dict[str, object]]:
        return [rule.to_dict() for rule in self.rules]

    @staticmethod
    def rules_from_dicts(raws: list[dict[str, object]]) -> tuple[FirewallRule, ...]:
        return tuple(FirewallRule.from_dict(raw) for raw in raws)
