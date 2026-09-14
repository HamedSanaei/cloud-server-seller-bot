"""Typed request/response models for the modern Leaseweb **VPS** API.

Every model here mirrors a schema in the local Leaseweb OpenAPI
documentation (``api_docs/leaseweb`` — tag ``VPS``, ``/publicCloud/v1/vps``)
field for field. Nothing is renamed, "fixed" or normalized:

- the documented (misspelled) ``automatedUnnulingAt`` null-route field keeps
  its exact wire name;
- ``prefixLength`` stays a string because the documentation types it as a
  string;
- the notification-setting create/update path carries the *client supplied*
  ``notificationSettingId`` exactly as documented;
- provider timestamps stay raw strings on the DTOs plus a lenient
  :func:`~cloud_platform.providers.leaseweb.models.parse_leaseweb_datetime`
  helper, so no provider value is silently rewritten.

Two documented required fields are tolerated as ``None`` on reads
(``vpsBase.reference`` and the nullable ``startedAt``): a reseller must not
lose a whole VPS read because Leaseweb reports an empty reference. This is
recorded in ``docs/leaseweb/VPS_API_COVERAGE.md``.

Secrets: console URLs and credential passwords are modelled with
``pydantic.SecretStr``, so ``repr()``, ``str()``, logs, traces and test
snapshot failures show ``**********`` instead of the value. A credential
value is only available through an explicit ``.get_secret_value()`` call at
the point of use.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import Field, SecretStr, field_validator

from cloud_platform.providers.leaseweb.models import (
    LeasewebModel,
    LeasewebRequestModel,
    Money,
    OpenStrEnum,
    parse_leaseweb_datetime,
)

__all__ = [
    "AcceptedVpsAction",
    "AttachIsoRequest",
    "ConsoleAccess",
    "ContractState",
    "ContractType",
    "CreateNotificationSettingRequest",
    "CreateSnapshotRequest",
    "CredentialDetail",
    "CredentialSummary",
    "CredentialType",
    "DataTrafficAggregation",
    "DataTrafficGranularity",
    "DataTrafficMetrics",
    "Datacenter",
    "ImageFlavour",
    "IsoRecord",
    "MonitoringState",
    "MonitoringStatusResult",
    "NetworkType",
    "NotificationAction",
    "NotificationChannel",
    "NotificationChannelRequest",
    "NotificationSetting",
    "NotificationThreshold",
    "NullRouteIpRequest",
    "RegionName",
    "ReinstallImage",
    "ReinstallRequest",
    "Snapshot",
    "SnapshotState",
    "StoreCredentialRequest",
    "StoredCredential",
    "TimePeriod",
    "TrafficMetric",
    "TrafficMetricSummary",
    "TrafficMetricValue",
    "TrafficUnit",
    "UpdateCredentialRequest",
    "UpdateIpRequest",
    "UpdateNotificationSettingRequest",
    "UpdateVpsRequest",
    "VpsContract",
    "VpsDataTrafficCommitment",
    "VpsDetail",
    "VpsImage",
    "VpsIp",
    "VpsIpDdos",
    "VpsIpDetails",
    "VpsMemory",
    "VpsNetworkSpeed",
    "VpsPackType",
    "VpsResourceValue",
    "VpsResources",
    "VpsState",
    "VpsSummary",
]


# ---------------------------------------------------------------------------
# Documented value sets (open: unknown future values survive verbatim)
# ---------------------------------------------------------------------------


class VpsState(OpenStrEnum):
    """``vpsState`` — the documented VPS power states."""

    RUNNING = "RUNNING"
    STARTING = "STARTING"
    STOPPED = "STOPPED"
    STOPPING = "STOPPING"


class VpsPackType(OpenStrEnum):
    """``vpsPackType`` — the documented VPS packages."""

    LEASEWEB_VPS_1 = "Leaseweb VPS 1"
    LEASEWEB_VPS_2 = "Leaseweb VPS 2"
    LEASEWEB_VPS_3 = "Leaseweb VPS 3"
    LEASEWEB_VPS_4 = "Leaseweb VPS 4"
    LEASEWEB_VPS_5 = "Leaseweb VPS 5"
    LEASEWEB_VPS_6 = "Leaseweb VPS 6"


class RegionName(OpenStrEnum):
    """``regionName`` — documented regions for the VPS product family."""

    EU_WEST_3 = "eu-west-3"
    US_EAST_1 = "us-east-1"
    EU_CENTRAL_1 = "eu-central-1"
    AP_SOUTHEAST_1 = "ap-southeast-1"
    US_WEST_1 = "us-west-1"
    EU_WEST_2 = "eu-west-2"
    CA_CENTRAL_1 = "ca-central-1"
    AP_NORTHEAST_1 = "ap-northeast-1"


class Datacenter(OpenStrEnum):
    """``datacenter`` — documented VPS datacenters."""

    LON_01 = "LON-01"
    MTL_02 = "MTL-02"
    AMS_01 = "AMS-01"
    FRA_01 = "FRA-01"
    WDC_02 = "WDC-02"
    SFO_12 = "SFO-12"
    SIN_01 = "SIN-01"


class CredentialType(OpenStrEnum):
    """``credentialType`` — the documented credential types for VPS."""

    OPERATING_SYSTEM = "OPERATING_SYSTEM"
    CONTROL_PANEL = "CONTROL_PANEL"


class NetworkType(OpenStrEnum):
    """``networkType`` — how an IP is attached."""

    INTERNAL = "INTERNAL"
    PUBLIC = "PUBLIC"


class MonitoringState(OpenStrEnum):
    """``monitoringStatus`` — documented monitoring states."""

    UP = "UP"
    DOWN = "DOWN"
    NOT_MONITORED = "NOT_MONITORED"
    UNKNOWN = "UNKNOWN"


class TimePeriod(OpenStrEnum):
    """``timePeriod`` — notification frequency."""

    DAY = "DAY"
    WEEK = "WEEK"
    MONTH = "MONTH"


class TrafficUnit(OpenStrEnum):
    """``unit`` — threshold units."""

    MB = "MB"
    GB = "GB"
    TB = "TB"


class NotificationAction(OpenStrEnum):
    """``action2`` — what happens when the threshold is exceeded."""

    POWER_OFF = "POWER_OFF"


class ImageFlavour(OpenStrEnum):
    """``flavour`` — documented standard image flavours."""

    UBUNTU = "ubuntu"
    DEBIAN = "debian"
    FREEBSD = "freebsd"
    CENTOS = "centos"
    ALMALINUX = "almalinux"
    ROCKYLINUX = "rockylinux"
    ARCHLINUX = "archlinux"
    WINDOWS = "windows"


class SnapshotState(OpenStrEnum):
    """``snapshot.state`` — documented snapshot states."""

    READY = "READY"
    CREATING = "CREATING"


class ContractType(OpenStrEnum):
    """``vpsContract.type`` — documented contract types."""

    HOURLY = "HOURLY"
    MONTHLY = "MONTHLY"


class ContractState(OpenStrEnum):
    """``vpsContract.state`` — documented contract states."""

    ACTIVE = "ACTIVE"
    DELETE_SCHEDULED = "DELETE_SCHEDULED"
    PENDING = "PENDING"
    INACTIVE = "INACTIVE"
    CANCELLED = "CANCELLED"


class DataTrafficGranularity(OpenStrEnum):
    """``granularity`` query parameter for datatraffic metrics."""

    FIVE_MINUTES = "5m"
    TEN_MINUTES = "10m"
    THIRTY_MINUTES = "30m"
    SIXTY_MINUTES = "60m"
    DAY = "DAY"


class DataTrafficAggregation(OpenStrEnum):
    """``aggregation`` query parameter (only ``SUM`` is documented)."""

    SUM = "SUM"


# ---------------------------------------------------------------------------
# Nested building blocks
# ---------------------------------------------------------------------------


class IsoRecord(LeasewebModel):
    """``iso`` — an ISO catalogue entry (also the attached ISO on a VPS)."""

    id: str
    name: str


class AcceptedVpsAction(LeasewebModel):
    """A documented ``202 Accepted`` VPS action that returns NO body.

    Leaseweb documents these operations as accepted-asynchronously; the
    marker records WHAT was accepted so the caller can audit it, without
    inventing provider payload fields.
    """

    vps_id: str
    #: The platform's own action label (not a Leaseweb vocabulary).
    action: str
    status_code: int = 202


class VpsImage(LeasewebModel):
    """``image`` — the OS image a VPS was installed from."""

    id: str
    name: str
    family: str
    flavour: ImageFlavour
    custom: bool


class VpsResourceValue(LeasewebModel):
    """A ``{value, unit}`` pair (CPU cores)."""

    value: int = 0
    unit: str = ""


class VpsMemory(LeasewebModel):
    """``memory`` — ``{value, unit}`` (GiB)."""

    value: Money = Decimal(0)
    unit: str = ""


class VpsNetworkSpeed(LeasewebModel):
    """``publicNetworkSpeed`` — network speed in Gbps."""

    value: int = 0
    unit: str = ""


class VpsResources(LeasewebModel):
    """``resources`` — the VPS's allocated resources."""

    cpu: VpsResourceValue | None = None
    memory: VpsMemory | None = None
    public_network_speed: VpsNetworkSpeed | None = None


class VpsDataTrafficCommitment(LeasewebModel):
    """``contract.dataTraffic`` — the data traffic commitment (TB or GB)."""

    value: Money = Decimal(0)
    unit: str = ""


class VpsContract(LeasewebModel):
    """``contract`` — the VPS contract/renewal information."""

    id: str
    type: ContractType
    state: ContractState
    term: int | None = None
    billing_frequency: int | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    sla: str | None = None
    control_panel: str | None = None
    in_modification: bool = False
    data_traffic: VpsDataTrafficCommitment | None = None

    @property
    def starts_at_dt(self) -> datetime | None:
        """``startsAt`` parsed to aware UTC (raw value stays on the DTO)."""
        return parse_leaseweb_datetime(self.starts_at)

    @property
    def ends_at_dt(self) -> datetime | None:
        """``endsAt`` parsed to aware UTC (raw value stays on the DTO)."""
        return parse_leaseweb_datetime(self.ends_at)


class VpsIp(LeasewebModel):
    """``ip2`` — one IP address of a VPS."""

    ip: str
    version: int
    network_type: NetworkType
    prefix_length: str = ""
    null_routed: bool = False
    main_ip: bool = False
    reverse_lookup: str | None = None

    @property
    def prefix_length_int(self) -> int | None:
        """``prefixLength`` parsed to int, or None when not numeric.

        The documented type is a string; the raw value is preserved.
        """
        try:
            return int(self.prefix_length)
        except (TypeError, ValueError):
            return None

    @property
    def is_ipv6(self) -> bool:
        return self.version == 6


class VpsIpDdos(LeasewebModel):
    """``ddos`` — DDoS protection details of one IP."""

    detection_profile: str | None = None
    protection_type: str | None = None


class VpsIpDetails(VpsIp):
    """``ipDetails`` — ``ip2`` plus its DDoS protection block."""

    ddos: VpsIpDdos | None = None


# ---------------------------------------------------------------------------
# VPS list / detail
# ---------------------------------------------------------------------------


class VpsSummary(LeasewebModel):
    """``vpsList`` — one VPS row of ``GET /publicCloud/v1/vps/``."""

    id: str
    pack: VpsPackType
    region: RegionName
    datacenter: Datacenter
    image: VpsImage
    state: VpsState
    has_public_ip_v4: bool = False
    root_disk_size: int = 0
    reference: str | None = None
    market_app_id: str | None = None
    started_at: str | None = None
    # ``Sequence`` (covariant) so :class:`VpsDetail` can narrow the element
    # type to the documented ``VpsIpDetails`` without breaking the override.
    ips: Sequence[VpsIp] = Field(default_factory=list)

    @property
    def started_at_dt(self) -> datetime | None:
        """``startedAt`` parsed to aware UTC (raw value stays on the DTO)."""
        return parse_leaseweb_datetime(self.started_at)

    def public_ip(self, version: int = 4) -> str | None:
        """The first PUBLIC IP of ``version``, or None."""
        for entry in self.ips:
            if entry.version == version and entry.network_type == NetworkType.PUBLIC:
                return entry.ip
        return None


class VpsDetail(VpsSummary):
    """``vpsDetails`` — full detail of ``GET /publicCloud/v1/vps/{vpsId}``."""

    ips: Sequence[VpsIpDetails] = Field(default_factory=list)
    iso: IsoRecord | None = None
    resources: VpsResources | None = None
    contract: VpsContract | None = None


# ---------------------------------------------------------------------------
# Console + credentials (secret-aware)
# ---------------------------------------------------------------------------


class ConsoleAccess(LeasewebModel):
    """``getConsoleAccessResult`` — a temporary VNC console URL.

    The URL is a TEMPORARY ACCESS TOKEN: it is stored as a
    :class:`~pydantic.SecretStr` so it cannot leak through ``repr()``,
    logs, traces, metric labels or failed-test output. Callers must use
    :meth:`reveal` at the single point where the customer is handed the
    link, and must never log the result.
    """

    url: SecretStr

    def reveal(self) -> str:
        """The console URL. Never log or serialize the result."""
        return self.url.get_secret_value()


class CredentialSummary(LeasewebModel):
    """``credential`` — a stored credential WITHOUT its secret value."""

    type: CredentialType
    username: str


class CredentialDetail(LeasewebModel):
    """``getCredentialResult`` — a credential including its password."""

    type: CredentialType
    username: str
    password: SecretStr

    def reveal_password(self) -> str:
        """The password. Never log or serialize the result."""
        return self.password.get_secret_value()


class StoredCredential(LeasewebModel):
    """``storeCredentialResult``/``updateCredentialResult``.

    Leaseweb echoes the stored credential (including the password) back.
    The value is secret-aware so it cannot leak.
    """

    type: CredentialType | None = None
    username: str | None = None
    password: SecretStr | None = None


class StoreCredentialRequest(LeasewebRequestModel):
    """``storeCredentialOpts`` — POST ``/credentials``."""

    type: CredentialType
    username: str
    password: SecretStr


class UpdateCredentialRequest(LeasewebRequestModel):
    """``updateCredentialOpts`` — PUT ``/credentials/{type}/{username}``."""

    password: SecretStr


# ---------------------------------------------------------------------------
# ISO attach/detach
# ---------------------------------------------------------------------------


class AttachIsoRequest(LeasewebRequestModel):
    """``attachIsoOpts`` — POST ``/attachIso``."""

    iso_id: str


# ---------------------------------------------------------------------------
# Reinstall
# ---------------------------------------------------------------------------


class ReinstallImage(LeasewebModel):
    """``imageDetails`` — one image available for reinstall."""

    id: str
    name: str
    family: str = ""
    flavour: ImageFlavour | None = None
    custom: bool = False
    storage_size: dict[str, Any] | None = None
    state: str | None = None
    state_reason: str | None = None
    region: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    version: str | None = None
    architecture: str | None = None
    market_apps: list[str] = Field(default_factory=list)
    storage_types: list[str] = Field(default_factory=list)
    min_disk_size: int | None = None


class ReinstallRequest(LeasewebRequestModel):
    """``reinstallResourceOpts`` — PUT ``/reinstall``.

    DESTRUCTIVE: recreates the VPS. Never call from a UI handler without
    ownership validation, authorization and explicit confirmation.
    """

    image_id: str
    market_app_id: str | None = None


# ---------------------------------------------------------------------------
# IP management
# ---------------------------------------------------------------------------


class UpdateIpRequest(LeasewebRequestModel):
    """``updateIPOpts2`` — PUT ``/ips/{ip}`` (reverse lookup)."""

    reverse_lookup: str


class NullRouteIpRequest(LeasewebRequestModel):
    """``nullRouteIPOpts2`` — POST ``/ips/{ip}/null``.

    The documented field name ``automatedUnnulingAt`` is intentionally kept
    verbatim (including its spelling); it is the provider's wire contract.
    """

    comment: str | None = None
    automated_unnuling_at: int | None = None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TrafficMetricValue(LeasewebModel):
    """``trafficMetricValue`` — one aggregated data point, in bytes."""

    value: int = 0
    timestamp: str | None = None

    @property
    def timestamp_dt(self) -> datetime | None:
        """``timestamp`` parsed to aware UTC (raw value stays on the DTO)."""
        return parse_leaseweb_datetime(self.timestamp)


class TrafficMetricSummary(LeasewebModel):
    """``trafficMetricSummary`` — average/expected/total/peak for one direction."""

    average: Money = Decimal(0)
    expected: Money = Decimal(0)
    total: Money = Decimal(0)
    peak: TrafficMetricValue | None = None


class TrafficMetric(LeasewebModel):
    """``trafficMetric`` — the series of one direction, plus its unit."""

    values: list[TrafficMetricValue] = Field(default_factory=list)
    unit: str = ""


class DataTrafficMetrics(LeasewebModel):
    """``getDataTrafficMetricsResult`` — preserved exactly as Leaseweb exposes it.

    ``metrics``/``summary`` are direction-keyed (``upPublic``/``downPublic``)
    and therefore kept as mappings (unknown future directions survive). The
    documented metadata (``from``/``to``/``granularity``/``aggregation``/
    ``unit``) is typed and preserved verbatim.

    Values are BYTES as integers. Customer billing must convert with integer
    or ``Decimal`` arithmetic — never binary float.
    """

    metrics: dict[str, TrafficMetric] = Field(default_factory=dict)
    # ``from`` is a Python keyword on the attribute and the documented wire
    # name is literally ``from``; the alias is therefore explicit (the
    # camelCase generator cannot round-trip the trailing underscore).
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    granularity: DataTrafficGranularity | None = None
    aggregation: DataTrafficAggregation | None = None
    unit: str = ""
    summary: dict[str, TrafficMetricSummary] = Field(default_factory=dict)

    @property
    def down_public(self) -> TrafficMetric | None:
        """The public inbound series, when reported."""
        return self.metrics.get("downPublic")

    @property
    def up_public(self) -> TrafficMetric | None:
        """The public outbound series, when reported."""
        return self.metrics.get("upPublic")

    def total_bytes(self) -> int:
        """Total bytes across every reported direction (integer only)."""
        return sum(point.value for metric in self.metrics.values() for point in metric.values)


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class Snapshot(LeasewebModel):
    """``snapshot`` — one VPS snapshot."""

    id: str
    display_name: str | None = None
    state: SnapshotState | None = None
    created: str | None = None

    @property
    def created_dt(self) -> datetime | None:
        """``created`` parsed to aware UTC (raw value stays on the DTO)."""
        return parse_leaseweb_datetime(self.created)


class CreateSnapshotRequest(LeasewebRequestModel):
    """``createSnapshotOpts`` — POST ``/snapshots``."""

    name: str


# ---------------------------------------------------------------------------
# Data-traffic notification settings
# ---------------------------------------------------------------------------


class NotificationThreshold(LeasewebModel):
    """``threshold`` — ``{value, unit}`` (tolerant: also used in requests)."""

    value: int
    unit: TrafficUnit


class NotificationChannel(LeasewebModel):
    """``channel`` — a response channel (includes the resolved ``contacts``)."""

    type: str
    contact_group: str
    contacts: list[str] = Field(default_factory=list)


class NotificationChannelRequest(LeasewebModel):
    """A create/update channel: ``{type, contactGroup}`` only."""

    type: str
    contact_group: str


class NotificationSetting(LeasewebModel):
    """``notificationSetting`` — one data-traffic notification setting."""

    id: str
    threshold: NotificationThreshold | None = None
    time_period: TimePeriod
    channels: list[NotificationChannel] = Field(default_factory=list)
    type: str = "DATA_TRAFFIC"
    action: NotificationAction | None = None


class CreateNotificationSettingRequest(LeasewebRequestModel):
    """``createNotificationSettingOpts``.

    The documented create path carries the CLIENT-SUPPLIED
    ``notificationSettingId`` as a path parameter (``POST
    /notificationSettings/dataTraffic/{notificationSettingId}``). That
    unusual shape is implemented verbatim; it is not an error in the docs.
    """

    threshold: NotificationThreshold
    time_period: TimePeriod
    action: NotificationAction | None = None
    channels: list[NotificationChannelRequest] = Field(min_length=1)


class UpdateNotificationSettingRequest(LeasewebRequestModel):
    """``updateNotificationSettingOpts`` — every field is optional."""

    threshold: NotificationThreshold | None = None
    time_period: TimePeriod | None = None
    action: NotificationAction | None = None
    channels: list[NotificationChannelRequest] | None = None

    @field_validator("channels")
    @classmethod
    def _channels_not_empty(
        cls, value: list[NotificationChannelRequest] | None
    ) -> list[NotificationChannelRequest] | None:
        if value is not None and not value:
            raise ValueError("channels must not be empty when provided")
        return value


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------


class MonitoringStatusResult(LeasewebModel):
    """``monitoringStatus`` — the VPS monitoring state."""

    status: MonitoringState | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# VPS update
# ---------------------------------------------------------------------------


class UpdateVpsRequest(LeasewebRequestModel):
    """``updateVpsOpts`` — PUT ``/publicCloud/v1/vps/{vpsId}``."""

    reference: str
