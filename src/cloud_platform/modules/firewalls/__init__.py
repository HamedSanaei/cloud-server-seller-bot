"""Firewalls module (M13-006): reusable, ownership-scoped firewall rulebooks."""

from .domain import (
    DuplicateFirewallError,
    Firewall,
    FirewallError,
    FirewallLimitError,
    FirewallNotFoundError,
    FirewallRule,
    InvalidFirewallRuleError,
)
from .repository import SqlAlchemyFirewallRepository
from .service import (
    FirewallRepository,
    FirewallService,
    ProviderFirewallPort,
    ensure_remote_firewall,
    firewall_port_of,
)

__all__ = [
    "DuplicateFirewallError",
    "Firewall",
    "FirewallError",
    "FirewallLimitError",
    "FirewallNotFoundError",
    "FirewallRepository",
    "FirewallRule",
    "FirewallService",
    "InvalidFirewallRuleError",
    "ProviderFirewallPort",
    "SqlAlchemyFirewallRepository",
    "ensure_remote_firewall",
    "firewall_port_of",
]
