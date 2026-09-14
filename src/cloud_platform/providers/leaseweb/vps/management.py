"""Leaseweb implementation of the provider-neutral VPS ports (§10/§22).

``LeaseWebVpsManagementMixin`` maps the typed modern-VPS client onto the
provider-neutral records in
:mod:`cloud_platform.providers.vps_ports`. Application/domain code can then
inspect and (where authorized) manage a Leaseweb VPS without knowing a single
Leaseweb URL, DTO field or error class.

Safety notes:

- Destructive methods (``reinstall_vps``, ``restore_vps_snapshot``,
  ``delete_vps_snapshot``, ``null_route_vps_ip``, ``detach_vps_iso``,
  ``attach_vps_iso``, ``reset_vps_password``) verify ONLY at the provider
  layer. Server ownership, authorization and explicit customer confirmation
  remain the responsibility of the calling application service, which must
  also route them through the durable operation ledger.
- ``get_console_session`` returns a secret-redacting record; nothing in this
  module logs a console URL, a password or an API key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cloud_platform.providers.leaseweb.vps.client import LeaseWebVpsApi
from cloud_platform.providers.leaseweb.vps.models import (
    CreateNotificationSettingRequest,
    CreateSnapshotRequest,
    NotificationSetting,
    NullRouteIpRequest,
    ReinstallRequest,
    UpdateIpRequest,
    UpdateNotificationSettingRequest,
    UpdateVpsRequest,
    VpsDetail,
    VpsIpDetails,
)
from cloud_platform.providers.vps_ports import (
    ConsoleSession,
    DataTrafficPoint,
    DataTrafficUsage,
    VpsActionAccepted,
    VpsInfo,
    VpsIpRecord,
    VpsIsoRecord,
    VpsMonitoringRecord,
    VpsNotificationRecord,
    VpsReinstallImage,
    VpsSnapshotRecord,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cloud_platform.providers.leaseweb.transport import LeasewebTransport

__all__ = ["LeaseWebVpsManagementMixin"]


class LeaseWebVpsManagementMixin:
    """Provider-neutral VPS management implemented over ``LeaseWebVpsApi``.

    The host class must provide ``_vps_api`` (the typed client) and
    ``_transport`` (the shared transport, used only for lifecycle).
    """

    _vps_api: LeaseWebVpsApi
    _transport: LeasewebTransport

    # ------------------------------------------------------------------
    # Inventory
    # ------------------------------------------------------------------

    async def get_vps_info(self, provider_server_id: str) -> VpsInfo | None:
        """Full VPS detail, or ``None`` when the provider reports no such VPS."""
        from cloud_platform.providers.errors import ProviderNotFound

        try:
            detail = await self._vps_api.get_vps(provider_server_id)
        except ProviderNotFound:
            return None
        return _info_from_detail(detail)

    async def list_vps_info(self) -> list[VpsInfo]:
        """Every VPS of the account, with its documented state."""
        rows = await self._vps_api.all_vps()
        return [_info_from_summary(row) for row in rows]

    async def rename_vps(self, provider_server_id: str, reference: str) -> VpsInfo:
        """Set the VPS ``reference`` (the only documented mutable field)."""
        detail = await self._vps_api.update_vps(
            provider_server_id, UpdateVpsRequest(reference=reference)
        )
        return _info_from_detail(detail)

    # ------------------------------------------------------------------
    # Power
    # ------------------------------------------------------------------

    async def start_vps(self, provider_server_id: str) -> VpsActionAccepted:
        accepted = await self._vps_api.start_vps(provider_server_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    async def stop_vps(self, provider_server_id: str) -> VpsActionAccepted:
        accepted = await self._vps_api.stop_vps(provider_server_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    async def reboot_vps(self, provider_server_id: str) -> VpsActionAccepted:
        accepted = await self._vps_api.reboot_vps(provider_server_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    # ------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------

    async def get_console_session(self, provider_server_id: str) -> ConsoleSession:
        """Temporary console URL (secret-redacting record)."""
        access = await self._vps_api.get_console_access(provider_server_id)
        return ConsoleSession(url=access.reveal())

    # ------------------------------------------------------------------
    # ISO + reinstall
    # ------------------------------------------------------------------

    async def list_vps_isos(self) -> list[VpsIsoRecord]:
        return [VpsIsoRecord(id=row.id, name=row.name) async for row in self._vps_api.iter_isos()]

    async def attach_vps_iso(self, provider_server_id: str, iso_id: str) -> VpsActionAccepted:
        accepted = await self._vps_api.attach_iso(provider_server_id, iso_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    async def detach_vps_iso(self, provider_server_id: str) -> VpsActionAccepted:
        accepted = await self._vps_api.detach_iso(provider_server_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    async def list_vps_reinstall_images(self, provider_server_id: str) -> list[VpsReinstallImage]:
        page = await self._vps_api.list_reinstall_images(provider_server_id)
        return [
            VpsReinstallImage(
                id=row.id,
                name=row.name,
                family=row.family or None,
                custom=row.custom,
                min_disk_gb=row.min_disk_size,
            )
            for row in page.items
        ]

    async def reinstall_vps(
        self, provider_server_id: str, image_id: str, market_app_id: str | None = None
    ) -> VpsActionAccepted:
        """DESTRUCTIVE: recreates the VPS (application-layer authorization required)."""
        accepted = await self._vps_api.reinstall(
            provider_server_id,
            ReinstallRequest(image_id=image_id, market_app_id=market_app_id),
        )
        return VpsActionAccepted(provider_server_id, accepted.action)

    # ------------------------------------------------------------------
    # IPs
    # ------------------------------------------------------------------

    async def list_vps_ips(self, provider_server_id: str) -> list[VpsIpRecord]:
        page = await self._vps_api.list_ips(provider_server_id)
        return [_ip_record(row) for row in page.items]

    async def get_vps_ip(self, provider_server_id: str, ip: str) -> VpsIpRecord:
        return _ip_record(await self._vps_api.get_ip(provider_server_id, ip))

    async def set_vps_ip_reverse_dns(
        self, provider_server_id: str, ip: str, reverse_lookup: str
    ) -> VpsIpRecord:
        record = await self._vps_api.update_ip(
            provider_server_id, ip, UpdateIpRequest(reverse_lookup=reverse_lookup)
        )
        return _ip_record(record)

    async def null_route_vps_ip(
        self,
        provider_server_id: str,
        ip: str,
        *,
        comment: str | None = None,
        automated_unnuling_hours: int | None = None,
    ) -> VpsIpRecord:
        """DESTRUCTIVE: null routes the IP (IPv4 only per the documentation)."""
        request = (
            NullRouteIpRequest(comment=comment, automated_unnuling_at=automated_unnuling_hours)
            if (comment is not None or automated_unnuling_hours is not None)
            else None
        )
        return _ip_record(await self._vps_api.null_route_ip(provider_server_id, ip, request))

    async def unnull_route_vps_ip(self, provider_server_id: str, ip: str) -> VpsIpRecord:
        return _ip_record(await self._vps_api.remove_ip_null_route(provider_server_id, ip))

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    async def list_vps_snapshots(self, provider_server_id: str) -> list[VpsSnapshotRecord]:
        return [
            _snapshot_record(row) async for row in self._vps_api.iter_snapshots(provider_server_id)
        ]

    async def get_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsSnapshotRecord:
        return _snapshot_record(await self._vps_api.get_snapshot(provider_server_id, snapshot_id))

    async def create_vps_snapshot(self, provider_server_id: str, name: str) -> VpsActionAccepted:
        accepted = await self._vps_api.create_snapshot(
            provider_server_id, CreateSnapshotRequest(name=name)
        )
        return VpsActionAccepted(provider_server_id, accepted.action)

    async def restore_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsActionAccepted:
        """DESTRUCTIVE: overwrites the running VPS with the snapshot."""
        accepted = await self._vps_api.restore_snapshot(provider_server_id, snapshot_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    async def delete_vps_snapshot(
        self, provider_server_id: str, snapshot_id: str
    ) -> VpsActionAccepted:
        """DESTRUCTIVE: deletes the snapshot."""
        accepted = await self._vps_api.delete_snapshot(provider_server_id, snapshot_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    # ------------------------------------------------------------------
    # Metrics / monitoring / credentials / notifications
    # ------------------------------------------------------------------

    async def get_vps_data_traffic(
        self,
        provider_server_id: str,
        *,
        from_: str,
        to: str,
        granularity: str,
        aggregation: str = "SUM",
    ) -> list[DataTrafficUsage]:
        """Provider-reported data traffic, in BYTES (integers only)."""
        metrics = await self._vps_api.get_data_traffic_metrics(
            provider_server_id,
            from_=from_,
            to=to,
            granularity=granularity,
            aggregation=aggregation,
        )
        usages: list[DataTrafficUsage] = []
        for direction, metric in sorted(metrics.metrics.items()):
            summary = metrics.summary.get(direction)
            usages.append(
                DataTrafficUsage(
                    direction=direction,
                    unit=metric.unit or metrics.unit,
                    points=tuple(
                        DataTrafficPoint(timestamp=point.timestamp_dt, bytes=point.value)
                        for point in metric.values
                    ),
                    total_bytes=_as_int(summary.total) if summary else 0,
                    average_bytes=_as_int(summary.average) if summary else 0,
                    expected_bytes=_as_int(summary.expected) if summary else 0,
                    peak_bytes=_as_int(summary.peak.value) if summary and summary.peak else 0,
                    metadata={
                        "from": metrics.from_,
                        "to": metrics.to,
                        "granularity": str(metrics.granularity or ""),
                        "aggregation": str(metrics.aggregation or ""),
                        "unit": metrics.unit,
                    },
                )
            )
        return usages

    async def get_vps_monitoring(self, provider_server_id: str) -> VpsMonitoringRecord:
        status = await self._vps_api.get_monitoring_status(provider_server_id)
        return VpsMonitoringRecord(
            status=str(status.status) if status.status is not None else None,
            description=status.description,
            documented=bool(status.status is None or status.status.is_documented),
        )

    async def enable_vps_monitoring(self, provider_server_id: str) -> None:
        await self._vps_api.enable_monitoring(provider_server_id)

    async def list_vps_credentials(self, provider_server_id: str) -> list[dict[str, str]]:
        """Credential REFERENCES (type + username) — never a password."""
        rows = await self._vps_api.list_credentials(provider_server_id)
        return [{"type": str(row.type), "username": row.username} for row in rows]

    async def reset_vps_password(self, provider_server_id: str) -> VpsActionAccepted:
        """DESTRUCTIVE: resets the password; the new value is read separately."""
        accepted = await self._vps_api.reset_password(provider_server_id)
        return VpsActionAccepted(provider_server_id, accepted.action)

    async def list_vps_notification_settings(
        self, provider_server_id: str
    ) -> list[VpsNotificationRecord]:
        page = await self._vps_api.list_data_traffic_notification_settings(provider_server_id)
        return [_notification_record(row) for row in page.items]

    async def get_vps_notification_setting(
        self, provider_server_id: str, notification_setting_id: str
    ) -> VpsNotificationRecord:
        row = await self._vps_api.get_data_traffic_notification_setting(
            provider_server_id, notification_setting_id
        )
        return _notification_record(row)

    async def create_vps_notification_setting(
        self,
        provider_server_id: str,
        notification_setting_id: str,
        payload: dict[str, Any],
    ) -> VpsNotificationRecord:
        """Create a setting whose id is SUPPLIED BY THE CALLER (documented shape)."""
        request = CreateNotificationSettingRequest.model_validate(payload)
        row = await self._vps_api.create_data_traffic_notification_setting(
            provider_server_id, notification_setting_id, request
        )
        return _notification_record(row)

    async def update_vps_notification_setting(
        self,
        provider_server_id: str,
        notification_setting_id: str,
        payload: dict[str, Any],
    ) -> VpsNotificationRecord:
        request = UpdateNotificationSettingRequest.model_validate(payload)
        row = await self._vps_api.update_data_traffic_notification_setting(
            provider_server_id, notification_setting_id, request
        )
        return _notification_record(row)

    async def delete_vps_notification_setting(
        self, provider_server_id: str, notification_setting_id: str
    ) -> None:
        await self._vps_api.delete_data_traffic_notification_setting(
            provider_server_id, notification_setting_id
        )


# ---------------------------------------------------------------------------
# Mapping helpers (Leaseweb DTO -> provider-neutral record)
# ---------------------------------------------------------------------------


def _info_from_summary(row: Any) -> VpsInfo:
    return VpsInfo(
        id=row.id,
        state=str(row.state),
        reference=row.reference,
        pack=str(row.pack),
        region=str(row.region),
        datacenter=str(row.datacenter),
        image_id=row.image.id,
        image_name=row.image.name,
        root_disk_gb=row.root_disk_size,
        started_at=row.started_at,
        metadata={
            "has_public_ip_v4": row.has_public_ip_v4,
            "market_app_id": row.market_app_id,
            "ips": [
                {"ip": ip.ip, "version": ip.version, "network_type": str(ip.network_type)}
                for ip in row.ips
            ],
            "documented_state": row.state.is_documented,
        },
    )


def _info_from_detail(detail: VpsDetail) -> VpsInfo:
    base = _info_from_summary(detail)
    contract = detail.contract
    iso_id = detail.iso.id if detail.iso else None
    metadata = dict(base.metadata)
    metadata["iso_id"] = iso_id
    if detail.resources is not None:
        metadata["resources"] = {
            "cpu": detail.resources.cpu.value if detail.resources.cpu else None,
            "cpu_unit": detail.resources.cpu.unit if detail.resources.cpu else None,
            "memory": str(detail.resources.memory.value) if detail.resources.memory else None,
            "memory_unit": detail.resources.memory.unit if detail.resources.memory else None,
            "public_network_speed": (
                detail.resources.public_network_speed.value
                if detail.resources.public_network_speed
                else None
            ),
        }
    return VpsInfo(
        id=base.id,
        state=base.state,
        reference=base.reference,
        pack=base.pack,
        region=base.region,
        datacenter=base.datacenter,
        image_id=base.image_id,
        image_name=base.image_name,
        root_disk_gb=base.root_disk_gb,
        started_at=base.started_at,
        contract_id=contract.id if contract else None,
        contract_state=str(contract.state) if contract else None,
        contract_ends_at=contract.ends_at if contract else None,
        contract_term=contract.term if contract else None,
        billing_frequency=contract.billing_frequency if contract else None,
        sla=contract.sla if contract else None,
        control_panel=contract.control_panel if contract else None,
        metadata=metadata,
    )


def _ip_record(row: VpsIpDetails) -> VpsIpRecord:
    return VpsIpRecord(
        ip=row.ip,
        version=row.version,
        network_type=str(row.network_type),
        null_routed=row.null_routed,
        main_ip=row.main_ip,
        reverse_lookup=row.reverse_lookup,
        prefix_length=row.prefix_length or None,
        metadata={
            "ddos": (
                {
                    "detection_profile": row.ddos.detection_profile,
                    "protection_type": row.ddos.protection_type,
                }
                if row.ddos
                else None
            )
        },
    )


def _snapshot_record(row: Any) -> VpsSnapshotRecord:
    return VpsSnapshotRecord(
        id=row.id,
        name=row.display_name,
        state=str(row.state) if row.state is not None else None,
        created_at=row.created,
    )


def _notification_record(row: NotificationSetting) -> VpsNotificationRecord:
    return VpsNotificationRecord(
        id=row.id,
        time_period=str(row.time_period) if row.time_period is not None else None,
        threshold_value=row.threshold.value if row.threshold else None,
        threshold_unit=str(row.threshold.unit) if row.threshold else None,
        action=str(row.action) if row.action is not None else None,
        channels=tuple(
            {
                "type": channel.type,
                "contact_group": channel.contact_group,
                "contacts": list(channel.contacts),
            }
            for channel in row.channels
        ),
        notification_type=row.type,
    )


def _as_int(value: Any) -> int:
    """Bytes -> int without float arithmetic on the money path."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
