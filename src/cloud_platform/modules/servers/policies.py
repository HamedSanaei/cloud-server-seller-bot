"""Customer capability policy for the Telegram My Servers experience.

The policy answers one question: *may this customer perform this operation on
this server right now?* It is deliberately provider-agnostic — the only
provider input is the adapter's advertised
:class:`~cloud_platform.providers.vps_ports.VpsCapabilities`, detected
structurally, so no ``if provider == "leaseweb"`` branch exists anywhere in the
application or UI layer (see ``scripts/check_domain_provider_branching.py``).

Two independent gates combine here:

1. **operator policy** — which capabilities the platform exposes to customers
   at all (``[features.server_management]`` configuration, conservative
   defaults for destructive/advanced operations);
2. **provider capabilities** — what the adapter can actually do.

An operation is allowed only when both agree, the local lifecycle state
permits it, and the server has a provider resource id to act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cloud_platform.modules.compute.domain import ServerLifecycleState
from cloud_platform.modules.servers.models import (
    CONFIRMATION_REQUIRED_OPERATIONS,
    CustomerServerState,
    ServerOperation,
)
from cloud_platform.providers.vps_ports import VpsCapabilities

__all__ = [
    "FEATURE_FLAG_KEYS",
    "ServerManagementPolicy",
    "UnsatisfiedOperation",
    "customer_state",
    "location_label",
    "operation_allowed",
]

#: Configuration keys of ``[features.server_management]`` mapped to the
#: operation groups they unlock. Order is the render order of the manage menu.
_GROUP_FLAGS: tuple[tuple[str, tuple[ServerOperation, ...]], ...] = (
    (
        "manager",
        (
            ServerOperation.VIEW,
            ServerOperation.REFRESH,
            ServerOperation.RENAME,
        ),
    ),
    (
        "power",
        (ServerOperation.START, ServerOperation.STOP, ServerOperation.REBOOT),
    ),
    ("console", (ServerOperation.CONSOLE,)),
    ("traffic", (ServerOperation.TRAFFIC,)),
    (
        "snapshots",
        (
            ServerOperation.SNAPSHOT_LIST,
            ServerOperation.SNAPSHOT_CREATE,
            ServerOperation.SNAPSHOT_RESTORE,
            ServerOperation.SNAPSHOT_DELETE,
        ),
    ),
    ("reinstall", (ServerOperation.REINSTALL,)),
    ("password_reset", (ServerOperation.PASSWORD_RESET,)),
    (
        "ip_management",
        (
            ServerOperation.IP_LIST,
            ServerOperation.IP_SET_RDNS,
            ServerOperation.IP_NULL_ROUTE,
            ServerOperation.IP_UNNULL_ROUTE,
        ),
    ),
    (
        "iso",
        (ServerOperation.ISO_LIST, ServerOperation.ISO_ATTACH, ServerOperation.ISO_DETACH),
    ),
    (
        "monitoring",
        (ServerOperation.MONITORING, ServerOperation.MONITORING_ENABLE),
    ),
    (
        # Commercial/self-service billing actions. Neither calls a provider API:
        # RENEW_NOW settles one period from the customer's own wallet and
        # AUTO_RENEW flips a durable preference. They live in their own group so
        # an operator can expose infrastructure management without exposing
        # money, or the other way round.
        "billing",
        (ServerOperation.RENEW_NOW, ServerOperation.AUTO_RENEW),
    ),
)

#: Every configuration key of the feature section (the CLI/docs surface).
FEATURE_FLAG_KEYS: tuple[str, ...] = tuple(flag for flag, _ in _GROUP_FLAGS)

#: Provider capability group each operation needs (None = provider-agnostic).
_OPERATION_CAPABILITY: dict[ServerOperation, str | None] = {
    ServerOperation.VIEW: "inventory",
    ServerOperation.REFRESH: "inventory",
    ServerOperation.RENAME: "inventory",
    ServerOperation.START: "power",
    ServerOperation.STOP: "power",
    ServerOperation.REBOOT: "power",
    ServerOperation.CONSOLE: "console",
    ServerOperation.TRAFFIC: "metrics",
    ServerOperation.SNAPSHOT_LIST: "snapshots",
    ServerOperation.SNAPSHOT_CREATE: "snapshots",
    ServerOperation.SNAPSHOT_RESTORE: "snapshots",
    ServerOperation.SNAPSHOT_DELETE: "snapshots",
    ServerOperation.REINSTALL: "reinstall",
    ServerOperation.PASSWORD_RESET: "credentials",  # pragma: allowlist secret
    ServerOperation.IP_LIST: "ips",
    ServerOperation.IP_SET_RDNS: "ips",
    ServerOperation.IP_NULL_ROUTE: "ips",
    ServerOperation.IP_UNNULL_ROUTE: "ips",
    ServerOperation.ISO_LIST: "iso",
    ServerOperation.ISO_ATTACH: "iso",
    ServerOperation.ISO_DETACH: "iso",
    ServerOperation.MONITORING: "monitoring",
    ServerOperation.MONITORING_ENABLE: "monitoring",
    # Commercial actions are provider-agnostic: they settle the LOCAL service
    # period, so they need no provider capability at all.
    ServerOperation.RENEW_NOW: None,
    ServerOperation.AUTO_RENEW: None,
}

#: Local lifecycle states in which each operation may be issued. ``None``
#: means "any stable state". Transitional states (REQUESTED/PROVISIONING/
#: DELETING) never allow mutations: the provider resource is not settled yet.
_STATE_GATES: dict[ServerOperation, frozenset[ServerLifecycleState]] = {
    # Operator-side lifecycle control of the local row.
    ServerOperation.VIEW: frozenset(),
    ServerOperation.REFRESH: frozenset(),
    # Power: the server must be RUNNING or STOPPED (already provisioned).
    ServerOperation.START: frozenset({ServerLifecycleState.STOPPED}),
    ServerOperation.STOP: frozenset({ServerLifecycleState.RUNNING}),
    ServerOperation.REBOOT: frozenset({ServerLifecycleState.RUNNING}),
    # Read-only or provider-side actions that need a provisioned resource.
    ServerOperation.CONSOLE: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.TRAFFIC: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.SNAPSHOT_LIST: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.SNAPSHOT_CREATE: frozenset({ServerLifecycleState.RUNNING}),
    ServerOperation.SNAPSHOT_RESTORE: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.SNAPSHOT_DELETE: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.REINSTALL: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.PASSWORD_RESET: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.IP_LIST: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.IP_SET_RDNS: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.IP_NULL_ROUTE: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.IP_UNNULL_ROUTE: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.ISO_LIST: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.ISO_ATTACH: frozenset({ServerLifecycleState.STOPPED}),
    ServerOperation.ISO_DETACH: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.MONITORING: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.MONITORING_ENABLE: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.RENAME: frozenset(
        {
            ServerLifecycleState.RUNNING,
            ServerLifecycleState.STOPPED,
            ServerLifecycleState.PROVISIONING,
        }
    ),
    # Commercial actions follow the LOCAL lifecycle, not the provider's: a
    # provisioned server (running or stopped) has a payable period, and the
    # preference itself is harmless to change at any stable state.
    ServerOperation.RENEW_NOW: frozenset(
        {ServerLifecycleState.RUNNING, ServerLifecycleState.STOPPED}
    ),
    ServerOperation.AUTO_RENEW: frozenset(),
}

#: Local provider state strings that mean "the provider says it is running".
_RUNNING_PROVIDER_STATES = frozenset({"RUNNING", "STARTED", "ACTIVE", "ON"})
_STOPPED_PROVIDER_STATES = frozenset({"STOPPED", "OFF", "SHUTOFF", "HALTED"})
#: Provider states that mean the server is converging on another state.
_TRANSITIONAL_PROVIDER_STATES = {
    "STARTING": CustomerServerState.STARTING,
    "PROVISIONING": CustomerServerState.PROVISIONING,
    "STOPPING": CustomerServerState.STOPPING,
    "REBOOTING": CustomerServerState.REBOOTING,
    "RESTARTING": CustomerServerState.REBOOTING,
    "REINSTALLING": CustomerServerState.REBOOTING,
}

_LOCAL_TO_CUSTOMER: dict[ServerLifecycleState, CustomerServerState] = {
    ServerLifecycleState.REQUESTED: CustomerServerState.PROVISIONING,
    ServerLifecycleState.PROVISIONING: CustomerServerState.PROVISIONING,
    ServerLifecycleState.RUNNING: CustomerServerState.RUNNING,
    ServerLifecycleState.STOPPED: CustomerServerState.STOPPED,
    ServerLifecycleState.ERROR: CustomerServerState.ERROR,
    ServerLifecycleState.MANUAL_REVIEW: CustomerServerState.PENDING_REVIEW,
    ServerLifecycleState.DELETE_REQUESTED: CustomerServerState.PENDING_REVIEW,
    ServerLifecycleState.DELETING: CustomerServerState.PENDING_REVIEW,
    ServerLifecycleState.DELETED: CustomerServerState.DELETED,
}

#: Datacenter prefix -> customer-friendly (flag, city). Unknown codes fall
#: back to the code itself rather than a guess.
_LOCATION_LABELS: dict[str, tuple[str, str]] = {
    "AMS": ("🇳🇱", "Amsterdam"),
    "FRA": ("🇩🇪", "Frankfurt"),
    "SFO": ("🇺🇸", "San Francisco"),
    "WDC": ("🇺🇸", "Washington"),
    "SIN": ("🇸🇬", "Singapore"),
    "TYO": ("🇯🇵", "Tokyo"),
    "HKG": ("🇭🇰", "Hong Kong"),
    "SYD": ("🇦🇺", "Sydney"),
    "MUC": ("🇩🇪", "Munich"),
    "LON": ("🇬🇧", "London"),
    "PAR": ("🇫🇷", "Paris"),
    "MAD": ("🇪🇸", "Madrid"),
    "MIL": ("🇮🇹", "Milan"),
    "WAW": ("🇵🇱", "Warsaw"),
    "STO": ("🇸🇪", "Stockholm"),
    "CHI": ("🇺🇸", "Chicago"),
    "ASH": ("🇺🇸", "Ashburn"),
    "PHX": ("🇺🇸", "Phoenix"),
    "DAL": ("🇺🇸", "Dallas"),
    "TOR": ("🇨🇦", "Toronto"),
    "FRA1": ("🇩🇪", "Frankfurt"),
    "DXB": ("🇦🇪", "Dubai"),
    "TEH": ("🇮🇷", "Tehran"),
    "THR": ("🇮🇷", "Tehran"),
}


@dataclass(frozen=True, slots=True)
class UnsatisfiedOperation:
    """Why an operation is not available (safe, non-identifying reasons)."""

    operation: ServerOperation
    reason: str


@dataclass(frozen=True, slots=True)
class ServerManagementPolicy:
    """Which capabilities the platform exposes to customers.

    Defaults are conservative: read-only and reversible actions plus the
    established power flow are on; ISO attach/detach is off because it changes
    the boot medium of a live server and is rarely a self-service need. Every
    flag can be turned on per deployment through
    ``[features.server_management]`` without a code change.
    """

    enabled: bool = True
    manager: bool = True
    power: bool = True
    console: bool = True
    traffic: bool = True
    snapshots: bool = True
    reinstall: bool = True
    password_reset: bool = True
    iso: bool = False
    ip_management: bool = True
    monitoring: bool = True
    billing: bool = True
    page_size: int = 5
    traffic_window_days: int = 30
    confirmation_ttl_seconds: int = 900

    @classmethod
    def from_settings(cls, settings: Any) -> ServerManagementPolicy:
        """Build the policy from the application settings (feature section)."""
        defaults = cls()
        return cls(
            enabled=bool(getattr(settings, "server_management_enabled", defaults.enabled)),
            manager=bool(getattr(settings, "server_management_manager", defaults.manager)),
            power=bool(getattr(settings, "server_management_power", defaults.power)),
            console=bool(getattr(settings, "server_management_console", defaults.console)),
            traffic=bool(getattr(settings, "server_management_traffic", defaults.traffic)),
            snapshots=bool(getattr(settings, "server_management_snapshots", defaults.snapshots)),
            reinstall=bool(getattr(settings, "server_management_reinstall", defaults.reinstall)),
            password_reset=bool(
                getattr(settings, "server_management_password_reset", defaults.password_reset)
            ),
            iso=bool(getattr(settings, "server_management_iso", defaults.iso)),
            ip_management=bool(
                getattr(settings, "server_management_ip_management", defaults.ip_management)
            ),
            monitoring=bool(getattr(settings, "server_management_monitoring", defaults.monitoring)),
            billing=bool(getattr(settings, "server_management_billing", defaults.billing)),
            page_size=int(
                getattr(settings, "server_management_page_size", defaults.page_size)
                or defaults.page_size
            ),
            traffic_window_days=int(
                getattr(settings, "server_management_traffic_window_days", None)
                or defaults.traffic_window_days
            ),
            confirmation_ttl_seconds=int(
                getattr(settings, "server_management_confirmation_ttl_seconds", None)
                or defaults.confirmation_ttl_seconds
            ),
        )

    def __post_init__(self) -> None:
        if self.page_size < 1:
            raise ValueError("page_size must be >= 1")
        if self.traffic_window_days < 1:
            raise ValueError("traffic_window_days must be >= 1")
        if self.confirmation_ttl_seconds < 30:
            raise ValueError("confirmation_ttl_seconds must be >= 30")

    # -- group lookups --------------------------------------------------

    @property
    def enabled_groups(self) -> tuple[str, ...]:
        """The capability groups this deployment exposes to customers."""
        if not self.enabled:
            return ()
        return tuple(flag for flag, _ in _GROUP_FLAGS if getattr(self, flag, False))

    def allows_group(self, flag: str) -> bool:
        return bool(self.enabled and getattr(self, flag, False))

    def allows(self, operation: ServerOperation) -> bool:
        """Whether the OPERATOR exposes ``operation`` to customers at all."""
        for flag, operations in _GROUP_FLAGS:
            if operation in operations:
                return self.allows_group(flag)
        return False

    def requires_confirmation(self, operation: ServerOperation) -> bool:
        """Whether ``operation`` needs a one-time confirmation token."""
        return operation in CONFIRMATION_REQUIRED_OPERATIONS


def operation_allowed(
    operation: ServerOperation,
    *,
    policy: ServerManagementPolicy,
    capabilities: VpsCapabilities,
    state: ServerLifecycleState,
) -> UnsatisfiedOperation | None:
    """Whether ``operation`` may be offered/executed; None when it may.

    Returns the specific unmet requirement so a screen can hide the control and
    the service can reject it with the same reason (the two can never
    disagree).
    """
    if not policy.enabled:
        return UnsatisfiedOperation(operation, "feature_disabled")
    if not policy.allows(operation):
        return UnsatisfiedOperation(operation, "not_exposed")
    capability = _OPERATION_CAPABILITY.get(operation)
    if capability is not None and not getattr(capabilities, capability, False):
        return UnsatisfiedOperation(operation, "provider_unsupported")
    gate = _STATE_GATES.get(operation)
    if gate and state not in gate:
        return UnsatisfiedOperation(operation, "state_not_allowed")
    return None


def customer_state(
    local_state: ServerLifecycleState, provider_state: str | None = None
) -> CustomerServerState:
    """Map a local lifecycle state (and provider state) to a customer state.

    The provider state wins when it is recognised, because it describes the
    actual infrastructure; the local state is the fallback so a pending or
    reviewing order still renders correctly. Unknown provider values degrade
    to the local mapping rather than raising.
    """
    if provider_state:
        upper = provider_state.strip().upper()
        if upper in _RUNNING_PROVIDER_STATES:
            return CustomerServerState.RUNNING
        if upper in _STOPPED_PROVIDER_STATES:
            return CustomerServerState.STOPPED
        if upper in _TRANSITIONAL_PROVIDER_STATES:
            return _TRANSITIONAL_PROVIDER_STATES[upper]
    return _LOCAL_TO_CUSTOMER.get(local_state, CustomerServerState.UNKNOWN)


def location_label(code: str | None) -> tuple[str, str] | None:
    """Map a datacenter code to ``(flag, city)``; None when unknown.

    Only the datacenter *code* is ever used for the lookup — never a provider
    resource id — and an unknown code is reported as unknown so the UI omits
    the location line rather than printing a raw internal code.
    """
    if not code:
        return None
    text = code.strip().upper()
    if text in _LOCATION_LABELS:
        return _LOCATION_LABELS[text]
    prefix = text.split("-")[0]
    return _LOCATION_LABELS.get(prefix)
