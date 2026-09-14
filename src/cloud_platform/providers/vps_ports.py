"""Provider-neutral VPS capability ports (LEASEWEB-VPS-API §10/§22).

Application and domain code talks to these records and protocols only — never
to a provider URL, a provider DTO or an HTTP client. A provider adapter that
supports the modern VPS surface implements the methods it needs and is
detected structurally via :func:`vps_capabilities_of` (the same
optional-capability pattern the repo already uses for power probes, rebuild,
rescue, snapshots and reverse DNS).

This module is provider-agnostic by construction: it imports nothing from any
adapter, so the architecture invariant "domain modules cannot import provider
adapters" keeps holding.

Secret discipline: :class:`ConsoleSession` and :class:`VpsCredentialRecord`
exist so callers can pass *references* to secret material around. The console
``url`` is a temporary access token and is redacted in ``repr``; credential
records deliberately carry NO secret value at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

__all__ = [
    "ConsoleSession",
    "DataTrafficPoint",
    "DataTrafficUsage",
    "VpsActionAccepted",
    "VpsCapabilities",
    "VpsConsoleProvider",
    "VpsCredentialProvider",
    "VpsInfo",
    "VpsInventoryProvider",
    "VpsIpManagementProvider",
    "VpsIpRecord",
    "VpsIsoProvider",
    "VpsIsoRecord",
    "VpsMetricsProvider",
    "VpsMonitoringProvider",
    "VpsMonitoringRecord",
    "VpsNotificationRecord",
    "VpsNotificationSettingsProvider",
    "VpsPowerProvider",
    "VpsReinstallImage",
    "VpsSnapshotManagementProvider",
    "VpsSnapshotRecord",
    "vps_capabilities_of",
]


@dataclass(frozen=True, slots=True)
class VpsInfo:
    """Provider-neutral VPS snapshot used by reconciliation (never billing).

    Prices/contract amounts are deliberately absent: this record describes
    provider STATE, not money. Provider-specific extras live in ``metadata``.
    """

    id: str
    state: str
    reference: str | None = None
    pack: str | None = None
    region: str | None = None
    datacenter: str | None = None
    image_id: str | None = None
    image_name: str | None = None
    root_disk_gb: int | None = None
    started_at: str | None = None
    contract_id: str | None = None
    contract_state: str | None = None
    contract_ends_at: str | None = None
    contract_term: int | None = None
    billing_frequency: int | None = None
    sla: str | None = None
    control_panel: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VpsIpRecord:
    """One IP of a VPS."""

    ip: str
    version: int
    network_type: str
    null_routed: bool = False
    main_ip: bool = False
    reverse_lookup: str | None = None
    prefix_length: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VpsSnapshotRecord:
    """One VPS snapshot."""

    id: str
    name: str | None = None
    state: str | None = None
    created_at: str | None = None
    size_gb: float | None = None


@dataclass(frozen=True, slots=True)
class VpsIsoRecord:
    """One ISO catalogue entry."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class VpsReinstallImage:
    """One image available for a reinstall."""

    id: str
    name: str
    family: str | None = None
    custom: bool = False
    min_disk_gb: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VpsActionAccepted:
    """An asynchronous provider action was accepted (no body returned)."""

    provider_server_id: str
    action: str


@dataclass(frozen=True, slots=True)
class ConsoleSession:
    """A temporary console access URL.

    The URL is a short-lived access token: ``repr``/``str`` are redacted so it
    cannot leak through logs, traces, audit metadata or a failed test.
    """

    url: str

    def __repr__(self) -> str:
        return f"ConsoleSession(url=<redacted len={len(self.url)}>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class DataTrafficPoint:
    """One provider-reported data point (bytes, integer only)."""

    timestamp: datetime | None
    bytes: int


@dataclass(frozen=True, slots=True)
class DataTrafficUsage:
    """One direction's data-traffic usage as reported by the provider.

    ``unit`` and ``aggregation`` are preserved verbatim. Application billing
    must convert ``total_bytes``/``average_bytes`` with integer or ``Decimal``
    arithmetic — never binary float.
    """

    direction: str
    unit: str
    points: tuple[DataTrafficPoint, ...] = ()
    total_bytes: int = 0
    average_bytes: int = 0
    expected_bytes: int = 0
    peak_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VpsMonitoringRecord:
    """Monitoring state of a VPS."""

    status: str | None
    description: str | None = None
    documented: bool = True


@dataclass(frozen=True, slots=True)
class VpsNotificationRecord:
    """One data-traffic notification setting (no secrets)."""

    id: str
    time_period: str | None = None
    threshold_value: int | None = None
    threshold_unit: str | None = None
    action: str | None = None
    channels: tuple[dict[str, Any], ...] = ()
    notification_type: str = "DATA_TRAFFIC"


class VpsInventoryProvider(Protocol):
    """Read + rename the provider's VPS inventory."""

    async def get_vps_info(self, provider_server_id: str) -> VpsInfo | None: ...
    async def list_vps_info(self) -> list[VpsInfo]: ...
    async def rename_vps(self, provider_server_id: str, reference: str) -> VpsInfo: ...


class VpsPowerProvider(Protocol):
    """Power operations (asynchronous: the provider answers 'accepted')."""

    async def start_vps(self, provider_server_id: str) -> VpsActionAccepted: ...
    async def stop_vps(self, provider_server_id: str) -> VpsActionAccepted: ...
    async def reboot_vps(self, provider_server_id: str) -> VpsActionAccepted: ...


class VpsConsoleProvider(Protocol):
    """Temporary console access (secret-bearing)."""

    async def get_console_session(self, provider_server_id: str) -> ConsoleSession: ...


class VpsIsoProvider(Protocol):
    """ISO catalogue plus attach/detach and the reinstall image catalogue."""

    async def list_vps_isos(self) -> list[VpsIsoRecord]: ...
    async def attach_vps_iso(self, provider_server_id: str, iso_id: str) -> VpsActionAccepted: ...
    async def detach_vps_iso(self, provider_server_id: str) -> VpsActionAccepted: ...
    async def list_vps_reinstall_images(
        self, provider_server_id: str
    ) -> list[VpsReinstallImage]: ...
    async def reinstall_vps(
        self, provider_server_id: str, image_id: str, market_app_id: str | None = None
    ) -> VpsActionAccepted: ...


class VpsIpManagementProvider(Protocol):
    """IP inventory, reverse DNS and null routing."""

    async def list_vps_ips(self, provider_server_id: str) -> list[VpsIpRecord]: ...
    async def get_vps_ip(self, provider_server_id: str, ip: str) -> VpsIpRecord: ...
    async def set_vps_ip_reverse_dns(
        self, provider_server_id: str, ip: str, reverse_lookup: str
    ) -> VpsIpRecord: ...
    async def null_route_vps_ip(
        self,
        provider_server_id: str,
        ip: str,
        *,
        comment: str | None = None,
        automated_unnuling_hours: int | None = None,
    ) -> VpsIpRecord: ...
    async def unnull_route_vps_ip(self, provider_server_id: str, ip: str) -> VpsIpRecord: ...


class VpsSnapshotManagementProvider(Protocol):
    """Snapshot inventory plus create/restore/delete (destructive)."""

    async def list_vps_snapshots(self, provider_server_id: str) -> list[VpsSnapshotRecord]: ...
    async def get_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsSnapshotRecord: ...
    async def create_vps_snapshot(
        self, provider_server_id: str, name: str
    ) -> VpsActionAccepted: ...
    async def restore_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsActionAccepted: ...
    async def delete_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsActionAccepted: ...


class VpsMetricsProvider(Protocol):
    """Provider-reported data-traffic metrics (bytes as integers)."""

    async def get_vps_data_traffic(
        self,
        provider_server_id: str,
        *,
        from_: str,
        to: str,
        granularity: str,
        aggregation: str = "SUM",
    ) -> list[DataTrafficUsage]: ...


class VpsMonitoringProvider(Protocol):
    """Monitoring state."""

    async def get_vps_monitoring(self, provider_server_id: str) -> VpsMonitoringRecord: ...
    async def enable_vps_monitoring(self, provider_server_id: str) -> None: ...


class VpsCredentialProvider(Protocol):
    """Credential REFERENCES only — never provider secret values."""

    async def list_vps_credentials(self, provider_server_id: str) -> list[dict[str, str]]: ...
    async def reset_vps_password(self, provider_server_id: str) -> VpsActionAccepted: ...


class VpsNotificationSettingsProvider(Protocol):
    """Data-traffic notification settings."""

    async def list_vps_notification_settings(
        self, provider_server_id: str
    ) -> list[VpsNotificationRecord]: ...
    async def get_vps_notification_setting(
        self, provider_server_id: str, notification_setting_id: str
    ) -> VpsNotificationRecord: ...
    async def create_vps_notification_setting(
        self,
        provider_server_id: str,
        notification_setting_id: str,
        payload: dict[str, Any],
    ) -> VpsNotificationRecord: ...
    async def update_vps_notification_setting(
        self,
        provider_server_id: str,
        notification_setting_id: str,
        payload: dict[str, Any],
    ) -> VpsNotificationRecord: ...
    async def delete_vps_notification_setting(
        self, provider_server_id: str, notification_setting_id: str
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class VpsCapabilities:
    """Which VPS capability groups a provider adapter actually implements.

    Probed structurally (method presence) so a provider that does not offer a
    group is simply ``False`` there, instead of raising at call time. Domain
    and UI code should branch on these flags — never on a provider name.
    """

    inventory: bool = False
    power: bool = False
    console: bool = False
    iso: bool = False
    reinstall: bool = False
    ips: bool = False
    snapshots: bool = False
    metrics: bool = False
    monitoring: bool = False
    credentials: bool = False
    notifications: bool = False

    @property
    def any(self) -> bool:
        return any(
            (
                self.inventory,
                self.power,
                self.console,
                self.iso,
                self.reinstall,
                self.ips,
                self.snapshots,
                self.metrics,
                self.monitoring,
                self.credentials,
                self.notifications,
            )
        )


def _has(provider: Any, *methods: str) -> bool:
    return all(callable(getattr(provider, method, None)) for method in methods)


def vps_capabilities_of(provider: Any) -> VpsCapabilities:
    """Detect the VPS capability groups ``provider`` implements."""
    return VpsCapabilities(
        inventory=_has(provider, "get_vps_info", "list_vps_info"),
        power=_has(provider, "start_vps", "stop_vps", "reboot_vps"),
        console=_has(provider, "get_console_session"),
        iso=_has(provider, "list_vps_isos", "attach_vps_iso", "detach_vps_iso"),
        reinstall=_has(provider, "list_vps_reinstall_images", "reinstall_vps"),
        ips=_has(
            provider,
            "list_vps_ips",
            "get_vps_ip",
            "set_vps_ip_reverse_dns",
            "null_route_vps_ip",
            "unnull_route_vps_ip",
        ),
        snapshots=_has(
            provider,
            "list_vps_snapshots",
            "get_vps_snapshot",
            "create_vps_snapshot",
            "restore_vps_snapshot",
            "delete_vps_snapshot",
        ),
        metrics=_has(provider, "get_vps_data_traffic"),
        monitoring=_has(provider, "get_vps_monitoring", "enable_vps_monitoring"),
        credentials=_has(provider, "list_vps_credentials", "reset_vps_password"),
        notifications=_has(
            provider,
            "list_vps_notification_settings",
            "get_vps_notification_setting",
            "create_vps_notification_setting",
            "update_vps_notification_setting",
            "delete_vps_notification_setting",
        ),
    )


def vps_inventory_of(provider: Any) -> VpsInventoryProvider:
    """Typed access to the inventory port (caller checked the capability)."""
    return cast(VpsInventoryProvider, provider)


def vps_power_of(provider: Any) -> VpsPowerProvider:
    """Typed access to the power port (caller checked the capability)."""
    return cast(VpsPowerProvider, provider)
