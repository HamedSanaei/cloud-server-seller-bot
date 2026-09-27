"""Hetzner Cloud's supported provider-neutral VPS management ports.

Only inventory, power and reinstall are exposed. In particular Hetzner snapshots
are billable images, and its console requires a separate password which the
provider-neutral ConsoleSession cannot represent; neither is advertised here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from cloud_platform.providers.errors import ProviderError, ProviderNotFound, ProviderOutcomeUnknown
from cloud_platform.providers.vps_ports import VpsActionAccepted, VpsInfo, VpsReinstallImage

if TYPE_CHECKING:
    from cloud_platform.providers.hetzner.client import HetznerCloudProvider


_SERVER_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SERVER_ID = re.compile(r"[1-9][0-9]*\Z")


def _server_id(value: str) -> str:
    if not isinstance(value, str) or not _SERVER_ID.fullmatch(value):
        raise ProviderError("invalid Hetzner server id")
    return value


def _info(item: Any) -> VpsInfo:
    if not isinstance(item, dict) or item.get("id") is None:
        raise ProviderError("Hetzner server response lacks identity")
    server_id = _server_id(str(item["id"]))
    name = item.get("name")
    status = item.get("status")
    if not isinstance(name, str) or not name or not isinstance(status, str) or not status:
        raise ProviderError("Hetzner server response lacks name or status")
    location = item.get("location")
    datacenter = item.get("datacenter")
    region = location.get("name") if isinstance(location, dict) else None
    dc_location = datacenter.get("location") if isinstance(datacenter, dict) else None
    nested_region = dc_location.get("name") if isinstance(dc_location, dict) else None
    if region and nested_region and region != nested_region:
        raise ProviderError("Hetzner server response has conflicting locations")
    server_type = item.get("server_type")
    image = item.get("image")
    public_net = item.get("public_net")
    addresses: dict[str, str] = {}
    if isinstance(public_net, dict):
        for version in ("ipv4", "ipv6"):
            entry = public_net.get(version)
            if isinstance(entry, dict) and isinstance(entry.get("ip"), str) and entry["ip"]:
                addresses[version] = entry["ip"]
    return VpsInfo(
        id=server_id,
        state=status,
        reference=name,
        pack=str(server_type.get("name") or server_type.get("id"))
        if isinstance(server_type, dict) and (server_type.get("name") or server_type.get("id"))
        else None,
        region=region or nested_region,
        datacenter=datacenter.get("name") if isinstance(datacenter, dict) else None,
        image_id=str(image["id"])
        if isinstance(image, dict) and image.get("id") is not None
        else None,
        image_name=image.get("name") if isinstance(image, dict) else None,
        root_disk_gb=server_type.get("disk")
        if isinstance(server_type, dict) and isinstance(server_type.get("disk"), int)
        else None,
        started_at=item.get("created"),
        metadata=addresses,
    )


class HetznerVpsManagement:
    """Adapter over one shared Hetzner credential holder/HTTP client.

    No own transport, token, retry or close lifecycle. Callers must verify
    ownership and gate destructive actions before invoking these port methods.
    """

    def __init__(self, provider: HetznerCloudProvider) -> None:
        self._provider = provider

    async def _server(self, provider_server_id: str) -> dict[str, Any]:
        payload = await self._provider._request("GET", f"/servers/{_server_id(provider_server_id)}")
        item = payload.get("server")
        if not isinstance(item, dict) or _info(item).id != provider_server_id:
            raise ProviderError("Hetzner server response identity mismatch")
        return item

    async def get_vps_info(self, provider_server_id: str) -> VpsInfo | None:
        try:
            return _info(await self._server(provider_server_id))
        except ProviderNotFound:
            return None

    async def list_vps_info(self) -> list[VpsInfo]:
        result: list[VpsInfo] = []
        page = 1
        for _ in range(1000):
            payload = await self._provider._request(
                "GET", "/servers", params={"page": page, "per_page": 50}
            )
            rows = payload.get("servers")
            meta = payload.get("meta")
            pagination = meta.get("pagination") if isinstance(meta, dict) else None
            if not isinstance(rows, list) or not isinstance(pagination, dict):
                raise ProviderError("Hetzner servers returned incomplete pagination")
            result.extend(_info(row) for row in rows)
            next_page = pagination.get("next_page")
            if next_page is None:
                return result
            if type(next_page) is not int or next_page <= page:
                raise ProviderError("Hetzner servers returned invalid pagination")
            page = next_page
        raise ProviderError("Hetzner servers exceeded safe pagination limit")

    async def rename_vps(self, provider_server_id: str, reference: str) -> VpsInfo:
        server_id = _server_id(provider_server_id)
        if not isinstance(reference, str) or not _SERVER_NAME.fullmatch(reference):
            raise ProviderError("invalid Hetzner server name")
        payload = await self._provider._mutation_request(
            "PUT", f"/servers/{server_id}", json={"name": reference}
        )
        item = payload.get("server")
        if not isinstance(item, dict):
            raise ProviderOutcomeUnknown("Hetzner rename response lacks server")
        try:
            info = _info(item)
        except ProviderError as exc:
            raise ProviderOutcomeUnknown("Hetzner rename response lacks identity") from exc
        if info.id != server_id or info.reference != reference:
            raise ProviderOutcomeUnknown("Hetzner rename response identity mismatch")
        return info

    async def start_vps(self, provider_server_id: str) -> VpsActionAccepted:
        return await self._power(provider_server_id, "poweron", "start")

    async def stop_vps(self, provider_server_id: str) -> VpsActionAccepted:
        return await self._power(provider_server_id, "poweroff", "stop")

    async def reboot_vps(self, provider_server_id: str) -> VpsActionAccepted:
        return await self._power(provider_server_id, "reboot", "reboot")

    async def _power(self, provider_server_id: str, action: str, label: str) -> VpsActionAccepted:
        server_id = _server_id(provider_server_id)
        payload = await self._provider._mutation_request(
            "POST", f"/servers/{server_id}/actions/{action}"
        )
        _accepted(payload, server_id)
        return VpsActionAccepted(server_id, label)

    async def list_vps_reinstall_images(self, provider_server_id: str) -> list[VpsReinstallImage]:
        server = await self._server(provider_server_id)
        server_type = server.get("server_type")
        if not isinstance(server_type, dict):
            raise ProviderError("Hetzner server type unavailable for image compatibility")
        architecture = server_type.get("architecture")
        disk = server_type.get("disk")
        if not isinstance(architecture, str) or not architecture or type(disk) is not int:
            raise ProviderError("Hetzner server architecture or disk unavailable")
        images: list[VpsReinstallImage] = []
        page = 1
        for _ in range(1000):
            payload = await self._provider._request(
                "GET", "/images", params={"type": "system", "page": page, "per_page": 50}
            )
            rows = payload.get("images")
            meta = payload.get("meta")
            pagination = meta.get("pagination") if isinstance(meta, dict) else None
            if not isinstance(rows, list) or not isinstance(pagination, dict):
                raise ProviderError("Hetzner images returned incomplete pagination")
            for image in rows:
                if not isinstance(image, dict):
                    raise ProviderError("Hetzner images returned malformed row")
                minimum = image.get("min_disk_size")
                if (
                    image.get("type") != "system"
                    or image.get("status") != "available"
                    or image.get("deprecated") is True
                    or image.get("deprecation")
                    or image.get("architecture") != architecture
                    or (type(minimum) is int and minimum > disk)
                    or (minimum is not None and type(minimum) is not int)
                    or image.get("id") is None
                ):
                    continue
                images.append(
                    VpsReinstallImage(
                        id=str(image["id"]),
                        name=str(image.get("name") or image.get("description") or image["id"]),
                        family=image.get("os_flavor"),
                        min_disk_gb=minimum,
                    )
                )
            next_page = pagination.get("next_page")
            if next_page is None:
                return images
            if type(next_page) is not int or next_page <= page:
                raise ProviderError("Hetzner images returned invalid pagination")
            page = next_page
        raise ProviderError("Hetzner images exceeded safe pagination limit")

    async def reinstall_vps(
        self, provider_server_id: str, image_id: str, market_app_id: str | None = None
    ) -> VpsActionAccepted:
        server_id = _server_id(provider_server_id)
        if market_app_id is not None:
            raise ProviderError("Hetzner market apps are not supported by reinstall")
        if not isinstance(image_id, str) or not _SERVER_ID.fullmatch(image_id):
            raise ProviderError("invalid Hetzner image id")
        if not any(
            image.id == image_id for image in await self.list_vps_reinstall_images(server_id)
        ):
            raise ProviderError("Hetzner system image is not installable on this server")
        payload = await self._provider._mutation_request(
            "POST", f"/servers/{server_id}/actions/rebuild", json={"image": image_id}
        )
        _accepted(payload, server_id)
        return VpsActionAccepted(server_id, "reinstall")


def _accepted(payload: dict[str, Any], server_id: str) -> None:
    action = payload.get("action")
    if not isinstance(action, dict) or action.get("id") is None:
        raise ProviderOutcomeUnknown("Hetzner action response has no action identity")
    resources = action.get("resources")
    if resources is not None and not isinstance(resources, list):
        raise ProviderOutcomeUnknown("Hetzner action response has invalid resources")
    if isinstance(resources, list):
        for resource in resources:
            if isinstance(resource, dict) and resource.get("type") == "server":
                if str(resource.get("id")) != server_id:
                    raise ProviderOutcomeUnknown("Hetzner action response server mismatch")
                break


__all__ = ["HetznerVpsManagement"]
