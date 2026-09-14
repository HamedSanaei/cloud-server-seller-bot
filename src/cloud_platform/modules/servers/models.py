"""Provider-neutral, customer-safe server models (Telegram My Servers).

These records are what the Telegram layer renders. They are deliberately
independent of every provider DTO: a provider adapter maps its own snapshot
into :class:`~cloud_platform.providers.vps_ports.VpsInfo` and the application
service maps *that* into the records here. No Telegram code ever sees a
Leaseweb field name, a raw equipment id or a provider enum.

Design rules encoded in this module:

- **No internal identifiers.** :class:`CustomerServerView` carries the local
  ``server_id`` only because the bot needs a stable key for its signed
  callbacks; provider resource ids stay inside the service.
- **UNKNOWN is a first-class state.** A provider that reports a state we do
  not model yet must not crash a screen: it maps to
  :attr:`CustomerServerState.UNKNOWN` (⚪) and the raw value stays available
  for diagnostics only.
- **Only genuinely-known values are set.** Every field but ``server_id`` and
  ``state`` is optional, so the UI omits what the provider did not return
  instead of inventing it.
- **Bytes stay integers.** Traffic values are ``int`` bytes; the UI formats
  them for display and no billing math ever happens here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

__all__ = [
    "BILLABLE_SERVER_OPERATIONS",
    "CONFIRMATION_REQUIRED_OPERATIONS",
    "DESTRUCTIVE_SERVER_OPERATIONS",
    "CustomerServerPage",
    "CustomerServerState",
    "CustomerServerView",
    "IpAddressView",
    "MonitoringView",
    "ServerActionOutcome",
    "ServerConsoleView",
    "ServerOperation",
    "ServerRenameView",
    "ServerRenewalView",
    "ServerSnapshotView",
    "TrafficUsageView",
    "format_bytes",
    "format_gb",
]


class CustomerServerState(StrEnum):
    """The only states a customer screen ever shows.

    Mapping is centralised in :func:`~cloud_platform.modules.servers.policies.
    customer_state`; the i18n key is ``servers.state.<value>``.
    """

    RUNNING = "running"
    STOPPED = "stopped"
    STARTING = "starting"
    STOPPING = "stopping"
    REBOOTING = "rebooting"
    PROVISIONING = "provisioning"
    PENDING_REVIEW = "pending_review"
    ERROR = "error"
    DELETED = "deleted"
    UNKNOWN = "unknown"

    @property
    def is_operable(self) -> bool:
        """Whether power actions may be offered in this state."""
        return self in (CustomerServerState.RUNNING, CustomerServerState.STOPPED)

    @property
    def is_transitional(self) -> bool:
        """Whether the provider is still converging on a stable state."""
        return self in (
            CustomerServerState.STARTING,
            CustomerServerState.STOPPING,
            CustomerServerState.REBOOTING,
            CustomerServerState.PROVISIONING,
        )


class ServerOperation(StrEnum):
    """Every customer-facing operation, as a policy identifier.

    The names are the values used by the capability policy and the
    confirmation model; they never appear in Telegram callbacks verbatim (the
    callbacks carry a short screen name instead).
    """

    VIEW = "view"
    REFRESH = "refresh"
    START = "start"
    STOP = "stop"
    REBOOT = "reboot"
    CONSOLE = "console"
    TRAFFIC = "traffic"
    SNAPSHOT_LIST = "snapshot_list"
    SNAPSHOT_CREATE = "snapshot_create"
    SNAPSHOT_RESTORE = "snapshot_restore"
    SNAPSHOT_DELETE = "snapshot_delete"
    REINSTALL = "reinstall"
    PASSWORD_RESET = "password_reset"
    IP_LIST = "ip_list"
    IP_SET_RDNS = "ip_set_rdns"
    IP_NULL_ROUTE = "ip_null_route"
    IP_UNNULL_ROUTE = "ip_unnull_route"
    ISO_LIST = "iso_list"
    ISO_ATTACH = "iso_attach"
    ISO_DETACH = "iso_detach"
    MONITORING = "monitoring"
    MONITORING_ENABLE = "monitoring_enable"
    RENAME = "rename"
    # Commercial operations. These never touch a provider API: RENEW_NOW settles
    # one period from the customer's own wallet, AUTO_RENEW flips a durable
    # preference. They are separate identifiers so the policy, the confirmation
    # model and the audit trail can treat money differently from infrastructure.
    RENEW_NOW = "renew_now"
    AUTO_RENEW = "auto_renew"


#: Operations that change or destroy provider state. The application service
#: refuses to run any of these without a consumed confirmation token, and the
#: Telegram layer always renders a confirmation screen first.
DESTRUCTIVE_SERVER_OPERATIONS: frozenset[ServerOperation] = frozenset(
    {
        ServerOperation.SNAPSHOT_RESTORE,
        ServerOperation.SNAPSHOT_DELETE,
        ServerOperation.REINSTALL,
        ServerOperation.PASSWORD_RESET,
        ServerOperation.IP_NULL_ROUTE,
        ServerOperation.ISO_ATTACH,
        ServerOperation.ISO_DETACH,
    }
)

#: Operations that move money. They are deliberately NOT part of
#: :data:`DESTRUCTIVE_SERVER_OPERATIONS` — nothing provider-side changes — but
#: they still demand an explicit one-time confirmation and an idempotent
#: settlement, because a double click must never charge twice.
BILLABLE_SERVER_OPERATIONS: frozenset[ServerOperation] = frozenset({ServerOperation.RENEW_NOW})

#: Operations that additionally require an explicit, one-time confirmation.
#: ``STOP`` is included because the customer loses access until they start it
#: again (the repository already confirmed power-off in the monthly UI).
CONFIRMATION_REQUIRED_OPERATIONS: frozenset[ServerOperation] = (
    DESTRUCTIVE_SERVER_OPERATIONS | BILLABLE_SERVER_OPERATIONS | {ServerOperation.STOP}
)


@dataclass(frozen=True, slots=True)
class ServerActionOutcome:
    """The result of one mutating customer action.

    ``replayed`` means the operation had already been accepted under the same
    idempotency key: the customer sees "already in progress/done" and the
    provider was NOT called a second time. ``outcome_unknown`` means the
    provider may have applied the change; the platform must not re-send and the
    UI must say so explicitly.
    """

    operation: ServerOperation
    accepted: bool
    replayed: bool = False
    outcome_unknown: bool = False
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerServerView:
    """Everything one customer may see about one server.

    ``server_id`` is the LOCAL id (the only identifier the callbacks carry);
    ``display_name`` is the customer's label, ``provider_display_name`` the
    provider-side reference when one exists.
    """

    server_id: UUID
    state: CustomerServerState
    display_name: str | None = None
    provider_display_name: str | None = None
    location_code: str | None = None
    location_label: str | None = None
    ip: str | None = None
    ipv6: str | None = None
    operating_system: str | None = None
    plan: str | None = None
    cpu: int | None = None
    ram_gb: int | None = None
    storage_gb: int | None = None
    traffic_limit: str | None = None
    traffic_used_bytes: int | None = None
    traffic_limit_bytes: int | None = None
    monitoring_status: str | None = None
    contract_started_at: str | None = None
    contract_ends_at: str | None = None
    next_renewal_at: datetime | None = None
    refresh_error: str | None = None
    #: True when the local row is authoritative for identity but the provider
    #: state could not be read (provider down / not yet provisioned).
    state_from_provider: bool = False
    extra_ips: tuple[IpAddressView, ...] = ()

    # -- commercial state (never an infrastructure state, §18) -------------
    #: The customer's COMMERCIAL status (``RenewalStatus`` value), or None when
    #: the platform holds no renewal record for this server. A server can be
    #: ``state=RUNNING`` and ``commercial_status="payment_due"`` at once.
    commercial_status: str | None = None
    #: Whether the customer can still settle the current period.
    commercial_payable: bool = False
    #: Durable automatic-renewal preference (None = no renewal record).
    auto_renew_enabled: bool | None = None
    #: End of the payable window once the period expired unpaid (grace only).
    grace_until: datetime | None = None
    #: The LOCAL sale price of the next period (never a current provider quote).
    renewal_price_minor: int | None = None
    renewal_currency: str | None = None

    @property
    def state_is_fresh(self) -> bool:
        return self.state_from_provider

    @property
    def has_commercial_record(self) -> bool:
        """Whether a renewal record exists for this server."""
        return self.commercial_status is not None


@dataclass(frozen=True, slots=True)
class CustomerServerPage:
    """One page of a customer's servers (stable, newest-first ordering)."""

    items: tuple[CustomerServerView, ...]
    page: int
    page_size: int
    total: int

    @property
    def pages(self) -> int:
        """Total number of pages (at least 1, so ``1 / 1`` renders)."""
        if self.page_size <= 0:
            return 1
        return max(1, -(-self.total // self.page_size))

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


@dataclass(frozen=True, slots=True)
class TrafficUsageView:
    """Customer-facing traffic usage (bytes as integers, never floats)."""

    period_from: str | None = None
    period_to: str | None = None
    granularity: str | None = None
    downloaded_bytes: int = 0
    uploaded_bytes: int = 0
    total_bytes: int = 0
    directions: tuple[str, ...] = ()
    limit_bytes: int | None = None
    limit_label: str | None = None
    separate_directions: bool = False
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ServerSnapshotView:
    """One snapshot, identified by a provider-side reference safe to display."""

    ref: str
    name: str | None = None
    state: str | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class IpAddressView:
    """One IP of a server (customer-safe: no internal network objects)."""

    ip: str
    version: int
    network_type: str | None = None
    main_ip: bool = False
    null_routed: bool = False
    reverse_lookup: str | None = None
    ddos_profile: str | None = None

    @property
    def display(self) -> str:
        return self.ip


@dataclass(frozen=True, slots=True)
class MonitoringView:
    """Customer-facing monitoring state (no provider implementation detail)."""

    enabled: bool
    status: str | None = None
    description: str | None = None
    documented: bool = True
    can_enable: bool = False


@dataclass(frozen=True, slots=True)
class ServerConsoleView:
    """A temporary console session.

    ``url`` is a short-lived access token, so the record is redacted in
    ``repr``/``str`` and the UI sends it as a one-tap link. It is never logged,
    never persisted and never placed in a business event.
    """

    url: str
    ttl_seconds: int | None = None

    def __repr__(self) -> str:
        return f"ServerConsoleView(url=<redacted len={len(self.url)}>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class ServerRenameView:
    """The outcome of a rename (local display name kept in step)."""

    server_id: UUID
    display_name: str


@dataclass(frozen=True, slots=True)
class ReinstallImageView:
    """One provider-offered reinstall image (never hard-coded)."""

    ref: str
    name: str
    family: str | None = None

    @property
    def is_free_of_charge_unknown(self) -> bool:
        """Whether the image carries no provider-provided pricing metadata."""
        return True


@dataclass(frozen=True, slots=True)
class ServerRenewalView:
    """The outcome of a customer-initiated commercial action.

    ``reason`` is a stable machine code (never a provider string) so the UI can
    pick its own wording: ``charged``, ``already_charged``, ``insufficient_funds``,
    ``not_payable``, ``manual_review_required``, ``no_renewal_record``,
    ``no_due_date``, ``auto_renew_on``, ``auto_renew_off``.
    """

    server_id: UUID
    reason: str
    settled: bool = False
    status: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    period_end: datetime | None = None
    grace_until: datetime | None = None
    auto_renew_enabled: bool | None = None


def format_bytes(value: int | None, *, unit: str = "B") -> str | None:
    """Render an integer byte count for a customer (binary steps, no float)."""
    if value is None:
        return None
    if value < 0:
        return None
    amount = int(value)
    for suffix in ("B", "KB", "MB", "GB", "TB", "PB"):
        if amount < 1024 or suffix == "PB":
            if suffix == "B" or unit != "B":
                return f"{amount} {suffix}"
            return f"{amount} {suffix}"
        amount = amount // 1024
    return f"{amount} PB"


def format_gb(value: int | None, *, unit: str = "GB") -> str | None:
    """Render a GB integer with its unit (``16 GB``)."""
    if value is None:
        return None
    return f"{int(value)} {unit}"


@dataclass(frozen=True, slots=True)
class ServerManagementSummary:
    """Counters for the list header/telemetry (never customer-visible)."""

    total: int = 0
    running: int = 0
    stopped: int = 0
    transitional: int = 0
    pending_review: int = 0
    unavailable: int = 0
    extras: dict[str, int] = field(default_factory=dict)


def utcnow() -> datetime:
    """Aware UTC now (kept here so tests can freeze one clock)."""
    return datetime.now(UTC)
