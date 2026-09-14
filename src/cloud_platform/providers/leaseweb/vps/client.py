"""Modern Leaseweb **VPS** API client (38 documented operations).

Implements the complete ``VPS`` tag of the local Leaseweb OpenAPI
documentation — ``/publicCloud/v1/vps...`` — as explicit, strongly typed
methods on top of the one shared :class:`LeasewebTransport`.

This is NOT the legacy ``Virtual-Servers`` API (``/virtualServers/...``),
which is a different, unmigrated product family and is deliberately NOT
implemented here (see ``docs/leaseweb/VPS_API_COVERAGE.md``).

Documented response semantics are preserved verbatim:

- ``start``/``stop``/``reboot``, ``resetPassword``, ``attachIso``,
  ``detachIso``, ``reinstall``, ``createSnapshot``, ``restoreSnapshot`` and
  ``deleteSnapshot`` answer ``202 Accepted`` with NO body: they return an
  :class:`~cloud_platform.providers.leaseweb.vps.models.AcceptedVpsAction`
  marker instead of inventing a payload;
- ``deleteCredentials``, ``deleteCredential``, ``deleteNotificationSetting``
  and ``enableMonitoring`` answer ``204 No Content``: they return ``None``;
- ``createNotificationSetting`` answers ``201`` with the created setting.

Safety:

- DESTRUCTIVE operations are classified in :data:`DESTRUCTIVE_OPERATIONS`
  and :data:`OPERATOR_ONLY_OPERATIONS`. This low-level client may expose
  them; a customer-facing service must still verify server ownership,
  authorization and explicit confirmation (and send them through the
  durable operation ledger where a mutation is chargeable or irreversible).
- Path parameters are validated and percent-encoded per segment, so a
  hostile ``username``/``ip``/``snapshotId`` can never alter the request
  shape (no path traversal, no query injection).
- Credential values, console URLs and passwords never appear in errors,
  logs, metric labels or traces: they are ``SecretStr`` fields and the
  transport redacts provider messages.
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import AsyncIterator
from typing import Any, TypeVar
from urllib.parse import quote

from pydantic import ValidationError

from cloud_platform.providers.leaseweb.errors import (
    LeasewebResponseError,
    LeasewebValidationError,
)
from cloud_platform.providers.leaseweb.models import (
    LeasewebModel,
    LeasewebPage,
    PaginationMetadata,
    collect_pages,
    paginate,
)
from cloud_platform.providers.leaseweb.transport import LeasewebTransport
from cloud_platform.providers.leaseweb.vps.models import (
    AcceptedVpsAction,
    AttachIsoRequest,
    ConsoleAccess,
    CreateNotificationSettingRequest,
    CreateSnapshotRequest,
    CredentialDetail,
    CredentialSummary,
    CredentialType,
    DataTrafficAggregation,
    DataTrafficGranularity,
    DataTrafficMetrics,
    IsoRecord,
    MonitoringStatusResult,
    NotificationSetting,
    NullRouteIpRequest,
    ReinstallImage,
    ReinstallRequest,
    Snapshot,
    StoreCredentialRequest,
    StoredCredential,
    UpdateCredentialRequest,
    UpdateIpRequest,
    UpdateNotificationSettingRequest,
    UpdateVpsRequest,
    VpsDetail,
    VpsIpDetails,
    VpsState,
    VpsSummary,
)

__all__ = [
    "CUSTOMER_EXPOSABLE_OPERATIONS",
    "DESTRUCTIVE_OPERATIONS",
    "OPERATOR_ONLY_OPERATIONS",
    "VPS_BASE_PATH",
    "LeaseWebVpsApi",
]

#: Root of the modern VPS API (documentation: tag ``VPS``).
VPS_BASE_PATH = "/publicCloud/v1/vps"

#: Operations that mutate or destroy provider state. A user-facing service
#: MUST require server ownership, authorization and explicit confirmation,
#: and MUST NOT expose them straight from a Telegram callback.
DESTRUCTIVE_OPERATIONS: frozenset[str] = frozenset(
    {
        "store_credential",
        "delete_credentials",
        "update_credential",
        "delete_credential",
        "reset_password",
        "reinstall",
        "restore_snapshot",
        "delete_snapshot",
        "null_route_ip",
        "attach_iso",
        "detach_iso",
        "delete_data_traffic_notification_setting",
    }
)

#: Operations that stay operator-only: never exposed to customers as-is.
OPERATOR_ONLY_OPERATIONS: frozenset[str] = frozenset(
    {
        "store_credential",
        "update_credential",
        "delete_credentials",
        "delete_credential",
        "reset_password",
        "reinstall",
        "restore_snapshot",
        "delete_snapshot",
        "null_route_ip",
        "attach_iso",
        "detach_iso",
        "create_data_traffic_notification_setting",
        "update_data_traffic_notification_setting",
        "delete_data_traffic_notification_setting",
        "enable_monitoring",
    }
)

#: Operations that may eventually be customer-facing (still ownership-checked).
CUSTOMER_EXPOSABLE_OPERATIONS: frozenset[str] = frozenset(
    {
        "start_vps",
        "stop_vps",
        "reboot_vps",
        "get_vps",
        "list_vps",
        "list_ips",
        "get_ip",
        "get_data_traffic_metrics",
        "list_snapshots",
        "get_snapshot",
        "create_snapshot",
        "get_console_access",
        "list_credentials",
        "get_monitoring_status",
        "list_isos",
        "list_reinstall_images",
    }
)

TModel = TypeVar("TModel", bound=LeasewebModel)


class LeaseWebVpsApi:
    """Typed client for the modern Leaseweb VPS API."""

    def __init__(self, transport: LeasewebTransport) -> None:
        self._transport = transport

    @property
    def transport(self) -> LeasewebTransport:
        return self._transport

    async def aclose(self) -> None:
        await self._transport.aclose()

    # ------------------------------------------------------------------
    # A. Power
    # ------------------------------------------------------------------

    async def start_vps(self, vps_id: str) -> AcceptedVpsAction:
        """``POST /publicCloud/v1/vps/{vpsId}/start`` (202, no body).

        Documented precondition: the VPS must be stopped; otherwise Leaseweb
        answers ``400`` (mapped to :class:`LeasewebValidationError`).
        """
        await self._push(vps_id, "start")
        return AcceptedVpsAction(vps_id=vps_id, action="start")

    async def stop_vps(self, vps_id: str) -> AcceptedVpsAction:
        """``POST /publicCloud/v1/vps/{vpsId}/stop`` (202, no body).

        Documented precondition: the VPS must be running.
        """
        await self._push(vps_id, "stop")
        return AcceptedVpsAction(vps_id=vps_id, action="stop")

    async def reboot_vps(self, vps_id: str) -> AcceptedVpsAction:
        """``POST /publicCloud/v1/vps/{vpsId}/reboot`` (202, no body).

        Documented precondition: the VPS must be running.
        """
        await self._push(vps_id, "reboot")
        return AcceptedVpsAction(vps_id=vps_id, action="reboot")

    # ------------------------------------------------------------------
    # B. Console
    # ------------------------------------------------------------------

    async def get_console_access(self, vps_id: str) -> ConsoleAccess:
        """``GET /publicCloud/v1/vps/{vpsId}/console``.

        The response holds a TEMPORARY console URL: it is returned as a
        secret-aware :class:`ConsoleAccess` and must never be logged,
        traced, cached in audit metadata or included in an exception.
        """
        payload = await self._transport.request("GET", self._path(vps_id, "console"))
        return self._parse(ConsoleAccess, payload, operation="get_console_access")

    # ------------------------------------------------------------------
    # C. Credentials
    # ------------------------------------------------------------------

    async def list_credentials(self, vps_id: str) -> list[CredentialSummary]:
        """``GET /publicCloud/v1/vps/{vpsId}/credentials``.

        Leaseweb documents no query parameters for this operation; the
        response is ``{credentials: [...], _metadata}`` and only the
        usernames are returned (values require :meth:`get_credential`).
        """
        payload = await self._transport.request("GET", self._path(vps_id, "credentials"))
        return self._items(payload, "credentials", CredentialSummary, operation="list_credentials")

    async def store_credential(
        self, vps_id: str, request: StoreCredentialRequest
    ) -> StoredCredential:
        """``POST /publicCloud/v1/vps/{vpsId}/credentials``.

        The body carries the password; Leaseweb echoes it back. Both the
        request and the response are secret-aware.
        """
        payload = await self._transport.request(
            "POST",
            self._path(vps_id, "credentials"),
            json=request.body(),
        )
        return self._parse(StoredCredential, payload, operation="store_credential")

    async def delete_credentials(self, vps_id: str) -> None:
        """``DELETE /publicCloud/v1/vps/{vpsId}/credentials`` (204).

        DESTRUCTIVE: removes every stored credential of the VPS.
        """
        await self._transport.request("DELETE", self._path(vps_id, "credentials"), mutating=True)

    async def list_credentials_by_type(
        self, vps_id: str, credential_type: CredentialType | str
    ) -> list[CredentialSummary]:
        """``GET /publicCloud/v1/vps/{vpsId}/credentials/{type}``."""
        payload = await self._transport.request(
            "GET", self._path(vps_id, "credentials", self._credential_type(credential_type))
        )
        return self._items(
            payload, "credentials", CredentialSummary, operation="list_credentials_by_type"
        )

    async def get_credential(
        self,
        vps_id: str,
        credential_type: CredentialType | str,
        username: str,
    ) -> CredentialDetail:
        """``GET /publicCloud/v1/vps/{vpsId}/credentials/{type}/{username}``.

        Returns the stored password as a secret-aware field.
        """
        payload = await self._transport.request(
            "GET",
            self._path(
                vps_id,
                "credentials",
                self._credential_type(credential_type),
                self._segment(username),
            ),
        )
        return self._parse(CredentialDetail, payload, operation="get_credential")

    async def update_credential(
        self,
        vps_id: str,
        credential_type: CredentialType | str,
        username: str,
        request: UpdateCredentialRequest,
    ) -> StoredCredential:
        """``PUT /publicCloud/v1/vps/{vpsId}/credentials/{type}/{username}``."""
        payload = await self._transport.request(
            "PUT",
            self._path(
                vps_id,
                "credentials",
                self._credential_type(credential_type),
                self._segment(username),
            ),
            json=request.body(),
        )
        return self._parse(StoredCredential, payload, operation="update_credential")

    async def delete_credential(
        self, vps_id: str, credential_type: CredentialType | str, username: str
    ) -> None:
        """``DELETE /publicCloud/v1/vps/{vpsId}/credentials/{type}/{username}`` (204).

        DESTRUCTIVE and OPERATOR-ONLY.
        """
        await self._transport.request(
            "DELETE",
            self._path(
                vps_id,
                "credentials",
                self._credential_type(credential_type),
                self._segment(username),
            ),
            mutating=True,
        )

    async def reset_password(self, vps_id: str) -> AcceptedVpsAction:
        """``POST /publicCloud/v1/vps/{vpsId}/resetPassword`` (202, no body).

        DESTRUCTIVE: the new credential must afterwards be read through the
        credentials endpoints. Never log the resulting value.
        """
        await self._push(vps_id, "resetPassword")
        return AcceptedVpsAction(vps_id=vps_id, action="reset_password")

    # ------------------------------------------------------------------
    # D. ISO management
    # ------------------------------------------------------------------

    async def list_isos(
        self, *, limit: int | None = None, offset: int | None = None
    ) -> LeasewebPage[IsoRecord]:
        """``GET /publicCloud/v1/vps/isos`` (account-wide ISO catalogue)."""
        payload = await self._transport.request(
            "GET", f"{VPS_BASE_PATH}/isos", params=_pagination(limit, offset)
        )
        return self._page(payload, "isos", IsoRecord, operation="list_isos")

    async def iter_isos(self, *, page_size: int = 100) -> AsyncIterator[IsoRecord]:
        """Iterate the ISO catalogue page by page."""

        async def fetch(limit: int, offset: int) -> LeasewebPage[IsoRecord]:
            return await self.list_isos(limit=limit, offset=offset)

        async for row in paginate(fetch, page_size=page_size):
            yield row

    async def attach_iso(self, vps_id: str, iso_id: str) -> AcceptedVpsAction:
        """``POST /publicCloud/v1/vps/{vpsId}/attachIso`` (202).

        Documented precondition: the VPS must NOT already have an ISO
        attached (otherwise a validation error is returned).
        """
        await self._transport.request(
            "POST",
            self._path(vps_id, "attachIso"),
            json=AttachIsoRequest(iso_id=iso_id).body(),
            mutating=True,
        )
        return AcceptedVpsAction(vps_id=vps_id, action="attach_iso")

    async def detach_iso(self, vps_id: str) -> AcceptedVpsAction:
        """``POST /publicCloud/v1/vps/{vpsId}/detachIso`` (202).

        Documented precondition: the VPS must HAVE an ISO attached. The
        documentation defines no request body.
        """
        await self._transport.request("POST", self._path(vps_id, "detachIso"), mutating=True)
        return AcceptedVpsAction(vps_id=vps_id, action="detach_iso")

    # ------------------------------------------------------------------
    # E. Reinstall
    # ------------------------------------------------------------------

    async def list_reinstall_images(
        self,
        vps_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
        standard: bool | None = None,
    ) -> LeasewebPage[ReinstallImage]:
        """``GET /publicCloud/v1/vps/{vpsId}/reinstall/images``."""
        params = _pagination(limit, offset)
        if standard is not None:
            params["standard"] = str(standard).lower()
        payload = await self._transport.request(
            "GET", self._path(vps_id, "reinstall", "images"), params=params
        )
        return self._page(payload, "images", ReinstallImage, operation="list_reinstall_images")

    async def reinstall(self, vps_id: str, request: ReinstallRequest) -> AcceptedVpsAction:
        """``PUT /publicCloud/v1/vps/{vpsId}/reinstall`` (202).

        DESTRUCTIVE: recreates the VPS, optionally with another image and
        marketplace app. Cannot run while the VPS has snapshots. The
        application layer must verify ownership, authorization and explicit
        confirmation before calling this.
        """
        await self._transport.request(
            "PUT",
            self._path(vps_id, "reinstall"),
            json=request.body(),
            mutating=True,
        )
        return AcceptedVpsAction(vps_id=vps_id, action="reinstall")

    # ------------------------------------------------------------------
    # F. IP management
    # ------------------------------------------------------------------

    async def list_ips(
        self,
        vps_id: str,
        *,
        version: int | None = None,
        null_routed: bool | None = None,
        ips: str | None = None,
    ) -> LeasewebPage[VpsIpDetails]:
        """``GET /publicCloud/v1/vps/{vpsId}/ips``.

        ``version`` is documented as ``4``/``6``, ``nullRouted`` as a
        boolean filter and ``ips`` as a ``|``-separated list of addresses
        (passed through verbatim, never re-normalized).
        """
        params: dict[str, Any] = {}
        if version is not None:
            if version not in (4, 6):
                raise LeasewebValidationError("ip version filter must be 4 or 6")
            params["version"] = version
        if null_routed is not None:
            params["nullRouted"] = str(null_routed).lower()
        if ips:
            params["ips"] = ips
        payload = await self._transport.request("GET", self._path(vps_id, "ips"), params=params)
        return self._page(payload, "ips", VpsIpDetails, operation="list_ips")

    async def iter_ips(self, vps_id: str, **filters: Any) -> AsyncIterator[VpsIpDetails]:
        """Iterate every IP of a VPS page by page."""

        async def fetch(limit: int, offset: int) -> LeasewebPage[VpsIpDetails]:
            page = await self.list_ips(vps_id, **filters)
            # The documented list endpoint exposes no pagination parameters,
            # so a single page always covers the collection.
            del limit, offset
            return page

        async for row in paginate(fetch, page_size=100):
            yield row

    async def get_ip(self, vps_id: str, ip: str) -> VpsIpDetails:
        """``GET /publicCloud/v1/vps/{vpsId}/ips/{ip}``."""
        payload = await self._transport.request("GET", self._path(vps_id, "ips", self._ip(ip)))
        return self._parse(VpsIpDetails, payload, operation="get_ip")

    async def update_ip(self, vps_id: str, ip: str, request: UpdateIpRequest) -> VpsIpDetails:
        """``PUT /publicCloud/v1/vps/{vpsId}/ips/{ip}`` — set the reverse lookup."""
        payload = await self._transport.request(
            "PUT",
            self._path(vps_id, "ips", self._ip(ip)),
            json=request.body(),
        )
        return self._parse(VpsIpDetails, payload, operation="update_ip")

    async def null_route_ip(
        self, vps_id: str, ip: str, request: NullRouteIpRequest | None = None
    ) -> VpsIpDetails:
        """``POST /publicCloud/v1/vps/{vpsId}/ips/{ip}/null``.

        DESTRUCTIVE (only works for IPv4, documented): cuts the IP off the
        network. The body is optional and documents ``comment`` and
        ``automatedUnnulingAt`` (hours until the route is removed).
        """
        body = request.body() if request is not None else None
        payload = await self._transport.request(
            "POST",
            self._path(vps_id, "ips", self._ip(ip), "null"),
            json=body,
            mutating=True,
        )
        return self._parse(VpsIpDetails, payload, operation="null_route_ip")

    async def remove_ip_null_route(self, vps_id: str, ip: str) -> VpsIpDetails:
        """``POST /publicCloud/v1/vps/{vpsId}/ips/{ip}/unnull`` (no body)."""
        payload = await self._transport.request(
            "POST",
            self._path(vps_id, "ips", self._ip(ip), "unnull"),
            mutating=True,
        )
        return self._parse(VpsIpDetails, payload, operation="remove_ip_null_route")

    # ------------------------------------------------------------------
    # G. Traffic metrics
    # ------------------------------------------------------------------

    async def get_data_traffic_metrics(
        self,
        vps_id: str,
        *,
        from_: str,
        to: str,
        granularity: DataTrafficGranularity | str,
        aggregation: DataTrafficAggregation | str = DataTrafficAggregation.SUM,
    ) -> DataTrafficMetrics:
        """``GET /publicCloud/v1/vps/{vpsId}/metrics/datatraffic``.

        Every documented parameter is required except ``aggregation``, whose
        only documented value is ``SUM`` (the default). Values are returned
        in BYTES as integers, with the provider's metadata preserved.
        """
        params = {
            "from": from_,
            "to": to,
            "granularity": str(granularity),
            "aggregation": str(aggregation),
        }
        payload = await self._transport.request(
            "GET", self._path(vps_id, "metrics", "datatraffic"), params=params
        )
        metrics = payload.get("metrics") if isinstance(payload, dict) else None
        metadata = payload.get("_metadata") if isinstance(payload, dict) else None
        if not isinstance(metrics, dict) or not isinstance(metadata, dict):
            raise LeasewebResponseError(
                "get_data_traffic_metrics: response is missing the documented "
                "metrics/_metadata fields"
            )
        merged: dict[str, Any] = dict(metadata)
        merged["metrics"] = metrics
        return self._parse(DataTrafficMetrics, merged, operation="get_data_traffic_metrics")

    # ------------------------------------------------------------------
    # H. Snapshots
    # ------------------------------------------------------------------

    async def list_snapshots(
        self,
        vps_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> LeasewebPage[Snapshot]:
        """``GET /publicCloud/v1/vps/{vpsId}/snapshots``."""
        payload = await self._transport.request(
            "GET", self._path(vps_id, "snapshots"), params=_pagination(limit, offset)
        )
        return self._page(payload, "snapshots", Snapshot, operation="list_snapshots")

    async def iter_snapshots(self, vps_id: str, *, page_size: int = 100) -> AsyncIterator[Snapshot]:
        """Iterate a VPS's snapshots page by page."""

        async def fetch(limit: int, offset: int) -> LeasewebPage[Snapshot]:
            return await self.list_snapshots(vps_id, limit=limit, offset=offset)

        async for row in paginate(fetch, page_size=page_size):
            yield row

    async def create_snapshot(
        self, vps_id: str, request: CreateSnapshotRequest
    ) -> AcceptedVpsAction:
        """``POST /publicCloud/v1/vps/{vpsId}/snapshots`` (202).

        Documented preconditions: the VPS must be running and at most ONE
        snapshot per VPS is allowed.
        """
        await self._transport.request(
            "POST",
            self._path(vps_id, "snapshots"),
            json=request.body(),
            mutating=True,
        )
        return AcceptedVpsAction(vps_id=vps_id, action="create_snapshot")

    async def get_snapshot(self, vps_id: str, snapshot_id: str) -> Snapshot:
        """``GET /publicCloud/v1/vps/{vpsId}/snapshots/{snapshotId}``."""
        payload = await self._transport.request(
            "GET", self._path(vps_id, "snapshots", self._uuid(snapshot_id, "snapshotId"))
        )
        return self._parse(Snapshot, payload, operation="get_snapshot")

    async def restore_snapshot(self, vps_id: str, snapshot_id: str) -> AcceptedVpsAction:
        """``PUT /publicCloud/v1/vps/{vpsId}/snapshots/{snapshotId}`` (202).

        DESTRUCTIVE: overwrites the running VPS with the snapshot contents.
        """
        await self._transport.request(
            "PUT",
            self._path(vps_id, "snapshots", self._uuid(snapshot_id, "snapshotId")),
            mutating=True,
        )
        return AcceptedVpsAction(vps_id=vps_id, action="restore_snapshot")

    async def delete_snapshot(self, vps_id: str, snapshot_id: str) -> AcceptedVpsAction:
        """``DELETE /publicCloud/v1/vps/{vpsId}/snapshots/{snapshotId}`` (202).

        DESTRUCTIVE.
        """
        await self._transport.request(
            "DELETE",
            self._path(vps_id, "snapshots", self._uuid(snapshot_id, "snapshotId")),
            mutating=True,
        )
        return AcceptedVpsAction(vps_id=vps_id, action="delete_snapshot")

    # ------------------------------------------------------------------
    # I. VPS list / details / update
    # ------------------------------------------------------------------

    async def list_vps(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
        vps_id: str | None = None,
        reference: str | None = None,
        ip: str | None = None,
        state: VpsState | str | None = None,
        pack: str | None = None,
        region: str | None = None,
    ) -> LeasewebPage[VpsSummary]:
        """``GET /publicCloud/v1/vps/`` with every documented filter.

        Filters: ``limit``, ``offset``, ``id``, ``reference``, ``ip``,
        ``state``, ``pack``, ``region``. Values are passed through verbatim
        (no normalization), except that ``ip`` is validated as an IP address
        because the documentation types it ``format: ip``.
        """
        params = _pagination(limit, offset)
        if vps_id is not None:
            params["id"] = vps_id
        if reference is not None:
            params["reference"] = reference
        if ip is not None:
            params["ip"] = self._validated_ip(ip)
        if state is not None:
            params["state"] = str(state)
        if pack is not None:
            params["pack"] = pack
        if region is not None:
            params["region"] = region
        payload = await self._transport.request("GET", f"{VPS_BASE_PATH}/", params=params)
        return self._page(payload, "vps", VpsSummary, operation="list_vps")

    async def list_vps_page(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        **filters: Any,
    ) -> LeasewebPage[VpsSummary]:
        """One explicit page of VPSes (thin, readable alias of :meth:`list_vps`)."""
        return await self.list_vps(limit=limit, offset=offset, **filters)

    async def iter_vps(self, *, page_size: int = 100, **filters: Any) -> AsyncIterator[VpsSummary]:
        """Iterate every VPS of the account page by page."""

        async def fetch(limit: int, offset: int) -> LeasewebPage[VpsSummary]:
            return await self.list_vps(limit=limit, offset=offset, **filters)

        async for row in paginate(fetch, page_size=page_size):
            yield row

    async def all_vps(self, *, page_size: int = 100, **filters: Any) -> list[VpsSummary]:
        """Every VPS of the account as a list (bounded pagination)."""

        async def fetch(limit: int, offset: int) -> LeasewebPage[VpsSummary]:
            return await self.list_vps(limit=limit, offset=offset, **filters)

        return await collect_pages(fetch, page_size=page_size)

    async def get_vps(self, vps_id: str) -> VpsDetail:
        """``GET /publicCloud/v1/vps/{vpsId}`` — full VPS detail."""
        payload = await self._transport.request("GET", self._path(vps_id))
        return self._parse(VpsDetail, payload, operation="get_vps")

    async def update_vps(
        self, vps_id: str, request: UpdateVpsRequest | None = None, *, reference: str | None = None
    ) -> VpsDetail:
        """``PUT /publicCloud/v1/vps/{vpsId}`` (documented body: ``reference``)."""
        if request is None:
            if reference is None:
                raise LeasewebValidationError(
                    "update_vps requires a reference (the only documented field)"
                )
            request = UpdateVpsRequest(reference=reference)
        payload = await self._transport.request(
            "PUT", self._path(vps_id), json=request.body(), mutating=True
        )
        return self._parse(VpsDetail, payload, operation="update_vps")

    # ------------------------------------------------------------------
    # J. Data-traffic notification settings
    # ------------------------------------------------------------------

    async def list_data_traffic_notification_settings(
        self,
        vps_id: str,
        *,
        limit: int | None = None,
        offset: int | None = None,
    ) -> LeasewebPage[NotificationSetting]:
        """``GET /publicCloud/v1/vps/{vpsId}/notificationSettings/dataTraffic``."""
        payload = await self._transport.request(
            "GET",
            self._path(vps_id, "notificationSettings", "dataTraffic"),
            params=_pagination(limit, offset),
        )
        return self._page(
            payload,
            "notificationSettings",
            NotificationSetting,
            operation="list_data_traffic_notification_settings",
        )

    async def get_data_traffic_notification_setting(
        self, vps_id: str, notification_setting_id: str
    ) -> NotificationSetting:
        """``GET .../notificationSettings/dataTraffic/{notificationSettingId}``."""
        payload = await self._transport.request(
            "GET",
            self._path(
                vps_id,
                "notificationSettings",
                "dataTraffic",
                self._uuid(notification_setting_id, "notificationSettingId"),
            ),
        )
        return self._parse(NotificationSetting, payload, operation="get_notification_setting")

    async def create_data_traffic_notification_setting(
        self,
        vps_id: str,
        notification_setting_id: str,
        request: CreateNotificationSettingRequest,
    ) -> NotificationSetting:
        """``POST .../notificationSettings/dataTraffic/{notificationSettingId}`` (201).

        The documented shape is unusual: the CLIENT supplies the new
        setting's id as a PATH parameter and the body carries only
        ``threshold``/``timePeriod``/``action``/``channels``. Implemented
        verbatim from the local documentation.
        """
        payload = await self._transport.request(
            "POST",
            self._path(
                vps_id,
                "notificationSettings",
                "dataTraffic",
                self._uuid(notification_setting_id, "notificationSettingId"),
            ),
            json=request.body(),
            mutating=True,
        )
        return self._parse(NotificationSetting, payload, operation="create_notification_setting")

    async def update_data_traffic_notification_setting(
        self,
        vps_id: str,
        notification_setting_id: str,
        request: UpdateNotificationSettingRequest,
    ) -> NotificationSetting:
        """``PUT .../notificationSettings/dataTraffic/{notificationSettingId}`` (200)."""
        payload = await self._transport.request(
            "PUT",
            self._path(
                vps_id,
                "notificationSettings",
                "dataTraffic",
                self._uuid(notification_setting_id, "notificationSettingId"),
            ),
            json=request.body(),
            mutating=True,
        )
        return self._parse(NotificationSetting, payload, operation="update_notification_setting")

    async def delete_data_traffic_notification_setting(
        self, vps_id: str, notification_setting_id: str
    ) -> None:
        """``DELETE .../notificationSettings/dataTraffic/{notificationSettingId}`` (204)."""
        await self._transport.request(
            "DELETE",
            self._path(
                vps_id,
                "notificationSettings",
                "dataTraffic",
                self._uuid(notification_setting_id, "notificationSettingId"),
            ),
            mutating=True,
        )

    # ------------------------------------------------------------------
    # K. Monitoring
    # ------------------------------------------------------------------

    async def get_monitoring_status(self, vps_id: str) -> MonitoringStatusResult:
        """``GET /publicCloud/v1/vps/{vpsId}/monitoring/status``."""
        payload = await self._transport.request("GET", self._path(vps_id, "monitoring", "status"))
        return self._parse(MonitoringStatusResult, payload, operation="get_monitoring_status")

    async def enable_monitoring(self, vps_id: str) -> None:
        """``POST /publicCloud/v1/vps/{vpsId}/monitoring/enable`` (204)."""
        await self._transport.request(
            "POST", self._path(vps_id, "monitoring", "enable"), mutating=True
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _push(self, vps_id: str, action: str) -> None:
        """A documented 202/no-body VPS action."""
        await self._transport.request("POST", self._path(vps_id, action), mutating=True)

    @staticmethod
    def _path(*segments: str) -> str:
        """Build a VPS path with each segment percent-encoded."""
        tail = "/".join(quote(segment, safe="") for segment in segments)
        return f"{VPS_BASE_PATH}/{tail}"

    @staticmethod
    def _segment(value: str) -> str:
        """Validate a free-form path segment (username)."""
        text = str(value or "").strip()
        if not text:
            raise LeasewebValidationError("path parameter must not be empty")
        return text

    @staticmethod
    def _credential_type(value: CredentialType | str) -> str:
        """Validate the documented ``type`` path parameter."""
        text = str(value)
        documented = {member.value for member in CredentialType}
        if text not in documented:
            raise LeasewebValidationError(
                f"credential type must be one of {sorted(documented)}, got {text!r}"
            )
        return text

    @staticmethod
    def _validated_ip(value: str) -> str:
        """Validate an address against the documented ``format: ip``."""
        try:
            return str(ipaddress.ip_address(str(value).strip()))
        except ValueError as exc:
            raise LeasewebValidationError(f"not a valid IP address: {value!r}") from exc

    @staticmethod
    def _ip(value: str) -> str:
        """Validate AND keep the caller's exact address text."""
        try:
            parsed = ipaddress.ip_address(str(value).strip())
        except ValueError as exc:
            raise LeasewebValidationError(f"not a valid IP address: {value!r}") from exc
        del parsed
        return str(value).strip()

    @staticmethod
    def _uuid(value: str, name: str) -> str:
        """Validate a documented ``format: uuid`` path parameter."""
        text = str(value).strip()
        try:
            uuid.UUID(text)
        except (ValueError, AttributeError, TypeError) as exc:
            raise LeasewebValidationError(f"{name} must be a UUID, got {value!r}") from exc
        return text

    @staticmethod
    def _parse(model: type[TModel], payload: Any, *, operation: str) -> TModel:
        """Validate a provider payload into a typed model.

        A schema mismatch raises :class:`LeasewebResponseError` carrying only
        FIELD NAMES and validation types — never field values, because a
        payload may contain credentials.
        """
        if not isinstance(payload, dict):
            raise LeasewebResponseError(
                f"{operation}: expected a JSON object, got {type(payload).__name__}"
            )
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise LeasewebResponseError(
                f"{operation}: provider response does not match the documented schema "
                f"({_safe_validation_summary(exc)})"
            ) from None

    @staticmethod
    def _items(payload: Any, key: str, model: type[TModel], *, operation: str) -> list[TModel]:
        if not isinstance(payload, dict):
            raise LeasewebResponseError(
                f"{operation}: expected a JSON object, got {type(payload).__name__}"
            )
        raw = payload.get(key)
        if not isinstance(raw, list):
            raise LeasewebResponseError(
                f"{operation}: response is missing the documented {key!r} array"
            )
        return [LeaseWebVpsApi._parse(model, row, operation=operation) for row in raw]

    def _page(
        self, payload: Any, key: str, model: type[TModel], *, operation: str
    ) -> LeasewebPage[TModel]:
        items = self._items(payload, key, model, operation=operation)
        metadata = self._metadata(payload, len(items))
        # ``TModel`` is a TypeVar, so the page is parametrized at runtime while
        # mypy keeps the declared return type.
        return LeasewebPage[TModel](items=items, metadata=metadata)

    @staticmethod
    def _metadata(payload: Any, page_size: int) -> PaginationMetadata | None:
        raw = payload.get("_metadata") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return None
        try:
            return PaginationMetadata.model_validate(raw)
        except ValidationError:
            # A malformed envelope must not hide a successful read.
            return PaginationMetadata(total_count=page_size, offset=0, limit=page_size)


def _pagination(limit: int | None, offset: int | None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    return params


def _safe_validation_summary(exc: ValidationError) -> str:
    """Field locations and validation types only — never values."""
    parts: list[str] = []
    for error in exc.errors()[:10]:
        loc = ".".join(str(part) for part in error.get("loc", ()))
        parts.append(f"{loc or '<root>'}: {error.get('type', 'invalid')}")
    return ", ".join(parts) or "invalid response"


#: Convenience alias used by the CLI and the adapter wiring.
LeasewebVpsApi = LeaseWebVpsApi
