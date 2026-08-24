from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    ProviderImage,
    ProviderLocation,
    ProviderPlan,
    ProviderServer,
    ProviderSnapshot,
)
from cloud_platform.providers.credentials import CredentialSource
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.health import AccountHealth
from cloud_platform.providers.hetzner.backoff import RateLimitBackoff, RateLimitPolicy


@dataclass(frozen=True, slots=True)
class RateLimitSnapshot:
    limit: int | None
    remaining: int | None
    reset_at_unix: int | None


class HetznerFirewallApi:
    """Firewall management against the Hetzner Cloud API (M13-006).

    A platform firewall rulebook maps 1:1 to a Hetzner firewall with the
    SAME name; syncs are idempotent by name (update-in-place, never
    duplicate). Rules use the Hetzner JSON shape:
    ``{"direction", "protocol", "port"?, "source_ips" | "destination_ips"}``.
    """

    def __init__(self, provider: HetznerCloudProvider) -> None:
        self._provider = provider

    @staticmethod
    def _rule_to_hetzner(rule: dict[str, Any]) -> dict[str, object]:
        direction = str(rule.get("direction"))
        out: dict[str, object] = {
            "direction": "in" if direction == "in" else "out",
            "protocol": str(rule.get("protocol")),
        }
        if rule.get("port"):
            out["port"] = str(rule["port"])
        cidrs = [str(c) for c in (rule.get("cidrs") or [])]
        if direction == "in":
            out["source_ips"] = cidrs
        else:
            out["destination_ips"] = cidrs
        return out

    async def list_firewalls(self) -> list[tuple[str, str]]:
        payload = await self._provider._request("GET", "/firewalls")
        return [(str(item["id"]), str(item["name"])) for item in payload.get("firewalls", [])]

    async def create_firewall(self, name: str, rules: list[dict[str, object]]) -> str:
        payload = await self._provider._request(
            "POST",
            "/firewalls",
            json={"name": name, "rules": [self._rule_to_hetzner(r) for r in rules]},
        )
        return str(payload["firewall"]["id"])

    async def update_firewall_rules(
        self, provider_firewall_id: str, rules: list[dict[str, object]]
    ) -> None:
        await self._provider._request(
            "PUT",
            f"/firewalls/{provider_firewall_id}",
            json={"rules": [self._rule_to_hetzner(r) for r in rules]},
        )

    async def delete_firewall(self, provider_firewall_id: str) -> None:
        try:
            await self._provider._request("DELETE", f"/firewalls/{provider_firewall_id}")
        except ProviderNotFound:
            return  # already gone - deletion is idempotent

    async def apply_to_servers(
        self, provider_firewall_id: str, provider_server_ids: list[str]
    ) -> None:
        await self._provider._request(
            "POST",
            f"/firewalls/{provider_firewall_id}/actions/apply_to_resources",
            json={
                "apply_to": [
                    {"type": "server", "server": {"id": int(sid)}} for sid in provider_server_ids
                ]
            },
        )

    async def remove_from_servers(
        self, provider_firewall_id: str, provider_server_ids: list[str]
    ) -> None:
        await self._provider._request(
            "POST",
            f"/firewalls/{provider_firewall_id}/actions/remove_from_resources",
            json={
                "remove_from": [
                    {"type": "server", "server": {"id": int(sid)}} for sid in provider_server_ids
                ]
            },
        )


class HetznerFloatingIpApi:
    """Floating IP lifecycle against the Hetzner Cloud API (M13-008).

    ``GET/POST /floating_ips``, ``POST /floating_ips/{id}/actions/assign``
    and ``.../unassign``, ``DELETE /floating_ips/{id}`` (404-idempotent).
    """

    def __init__(self, provider: HetznerCloudProvider) -> None:
        self._provider = provider

    async def list_floating_ips(self) -> list[tuple[str, str]]:
        payload = await self._provider._request("GET", "/floating_ips")
        return [
            (str(item["id"]), str(item.get("ip", ""))) for item in payload.get("floating_ips", [])
        ]

    async def create_floating_ip(self, location_id: str) -> tuple[str, str]:
        payload = await self._provider._request(
            "POST", "/floating_ips", json={"home_location": location_id}
        )
        created = payload["floating_ip"]
        return str(created["id"]), str(created["ip"])

    async def assign_floating_ip(self, provider_ip_id: str, provider_server_id: str) -> None:
        await self._provider._request(
            "POST",
            f"/floating_ips/{provider_ip_id}/actions/assign",
            json={"server": int(provider_server_id)},
        )

    async def unassign_floating_ip(self, provider_ip_id: str) -> None:
        await self._provider._request("POST", f"/floating_ips/{provider_ip_id}/actions/unassign")

    async def delete_floating_ip(self, provider_ip_id: str) -> None:
        try:
            await self._provider._request("DELETE", f"/floating_ips/{provider_ip_id}")
        except ProviderNotFound:
            return  # already gone - deletion is idempotent


class HetznerSshKeyApi:
    """SSH-key management against the Hetzner Cloud API (M13-001).

    ``GET /ssh_keys`` / ``POST /ssh_keys`` / ``DELETE /ssh_keys/{id}``.
    Uploads are idempotent-by-caller: the sync layer reuses an existing
    same-name/same-fingerprint key and never re-uploads it.
    """

    def __init__(self, provider: HetznerCloudProvider) -> None:
        self._provider = provider

    async def list_ssh_keys(self) -> list[tuple[str, str, str]]:
        payload = await self._provider._request("GET", "/ssh_keys")
        return [
            (
                str(item["id"]),
                str(item["name"]),
                str(item.get("fingerprint", "")),
            )
            for item in payload.get("ssh_keys", [])
        ]

    async def upload_ssh_key(self, name: str, public_key: str) -> str:
        payload = await self._provider._request(
            "POST", "/ssh_keys", json={"name": name, "public_key": public_key}
        )
        return str(payload["ssh_key"]["id"])

    async def delete_ssh_key(self, provider_key_id: str) -> None:
        try:
            await self._provider._request("DELETE", f"/ssh_keys/{provider_key_id}")
        except ProviderNotFound:
            # already gone at the provider - deletion is idempotent
            return


class HetznerCloudProvider:
    key = "hetzner"
    capabilities = frozenset(
        {
            Capability.COMPUTE,
            Capability.POWER,
            Capability.REBUILD,
            Capability.RESCUE,
            Capability.SNAPSHOT,
            Capability.BACKUP,
            Capability.FIREWALL,
            Capability.NETWORK,
            Capability.VOLUME,
            Capability.FLOATING_IP,
            Capability.PRIMARY_IP,
            Capability.RDNS,
        }
    )

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.hetzner.cloud/v1",
        rate_limit_policy: RateLimitPolicy | None = None,
        credential_source: CredentialSource | None = None,
    ) -> None:
        if credential_source is not None:
            # Runtime-rotatable (M10-008): the Authorization header is set per
            # request from the source; ``token`` is only the initial value.
            headers: dict[str, str] = {}
        else:
            headers = {"Authorization": f"Bearer {token}"}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(30.0),
        )
        self._credential_source = credential_source
        self.last_rate_limit = RateLimitSnapshot(None, None, None)
        self._backoff = RateLimitBackoff(rate_limit_policy or RateLimitPolicy())
        # M13-001/M13-006/M13-008: capability-probed management ports
        self.ssh_keys = HetznerSshKeyApi(self)
        self.firewalls = HetznerFirewallApi(self)
        self.floating_ips = HetznerFloatingIpApi(self)

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def close(self) -> None:
        await self._client.aclose()

    async def check_health(self) -> AccountHealth:
        """Check the health of this Hetzner account.

        Makes a lightweight API call to verify credentials and connectivity.

        Returns:
            AccountHealth status
        """
        try:
            # Use a lightweight endpoint to check connectivity
            await self._request("GET", "/datacenters")
            return AccountHealth.HEALTHY
        except Exception:
            return AccountHealth.UNHEALTHY

    async def verify_credential(self, candidate: str) -> None:
        """Verify a CANDIDATE token with a read-only call (M10-008).

        The candidate is sent only for this one request; the live
        credential (if any) is untouched. Raises ``ProviderAuthError`` when
        the candidate is rejected (401/403), other ``ProviderError``
        subclasses on other failures.
        """
        response = await self._client.request(
            "GET", "/datacenters", headers=self._auth_headers(candidate)
        )
        if response.status_code == 401 or response.status_code == 403:
            raise ProviderAuthError(_error_message(response))
        if response.status_code == 429:
            raise ProviderRateLimited(_error_message(response))
        if response.status_code >= 500:
            raise ProviderUnavailable(_error_message(response))
        if response.is_error:
            raise ProviderError(_error_message(response))

    async def list_locations(self) -> list[ProviderLocation]:
        payload = await self._request("GET", "/locations")
        return [
            ProviderLocation(
                id=str(item["id"]),
                name=item["name"],
                country_code=item["country"],
                city=item.get("city"),
                network_zone=item.get("network_zone"),
                metadata={"description": item.get("description")},
            )
            for item in payload.get("locations", [])
        ]

    async def list_plans(self) -> list[ProviderPlan]:
        payload = await self._request("GET", "/server_types")
        return [
            ProviderPlan(
                id=str(item["id"]),
                name=item["name"],
                architecture=item.get("architecture", "unknown"),
                vcpu=int(item["cores"]),
                memory_mb=int(float(item["memory"]) * 1024),
                disk_gb=int(item["disk"]),
                metadata={"cpu_type": item.get("cpu_type")},
            )
            for item in payload.get("server_types", [])
        ]

    async def list_images(self) -> list[ProviderImage]:
        payload = await self._request("GET", "/images", params={"type": "system"})
        return [
            ProviderImage(
                id=str(item["id"]),
                name=item.get("name") or item.get("description") or str(item["id"]),
                os_family=item.get("os_flavor") or "unknown",
                architecture=item.get("architecture", "unknown"),
                metadata={"os_version": item.get("os_version")},
            )
            for item in payload.get("images", [])
        ]

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        try:
            payload = await self._request("GET", f"/servers/{provider_server_id}")
        except ProviderNotFound:
            return None
        return self._map_server(payload["server"])

    async def list_servers(self) -> list[ProviderServer]:
        servers: list[ProviderServer] = []
        page = 1
        while True:
            payload = await self._request("GET", "/servers", params={"page": page})
            servers.extend(self._map_server(item) for item in payload.get("servers", []))
            pagination = (payload.get("meta") or {}).get("pagination") or {}
            next_page = pagination.get("next_page")
            if not isinstance(next_page, int) or next_page <= page:
                return servers
            page = next_page

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer:
        # Hetzner does not provide a generic Idempotency-Key contract for this endpoint.
        # We therefore persist our own operation before the provider call, label resources
        # with operation identity, and reconcile on timeout/retry. The key is intentionally
        # accepted by the provider port so every adapter must participate in idempotency.
        body: dict[str, Any] = {
            "name": request.name,
            "server_type": request.plan_id,
            "image": request.image_id,
            "location": request.location_id,
            "ssh_keys": list(request.ssh_key_ids),
            "labels": {**request.labels, "platform-operation": idempotency_key.value[:63]},
        }
        if request.user_data is not None:
            body["user_data"] = request.user_data
        payload = await self._request("POST", "/servers", json=body)
        return self._map_server(payload["server"])

    async def delete_server(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        try:
            await self._request("DELETE", f"/servers/{provider_server_id}")
        except ProviderNotFound:
            return

    async def power_on(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/servers/{provider_server_id}/actions/poweron")

    async def power_off(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/servers/{provider_server_id}/actions/poweroff")

    async def reboot(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/servers/{provider_server_id}/actions/reboot")

    async def set_reverse_dns(self, provider_server_id: str, ip: str, ptr: str | None) -> None:
        """Set/reset the reverse DNS (PTR) of one server IP (M13-007).

        ``POST /servers/{id}/actions/change_dns_ptr`` with ``dns_ptr``
        (``None`` resets the automatic assignment). Naturally idempotent:
        re-setting an identical PTR is a no-op on the provider side.
        """
        await self._request(
            "POST",
            f"/servers/{provider_server_id}/actions/change_dns_ptr",
            json={"ip": ip, "dns_ptr": ptr},
        )

    async def rebuild_server(
        self, provider_server_id: str, image_id: str, idempotency_key: IdempotencyKey | None = None
    ) -> str:
        """Re-image the server (M13-002): ``POST /servers/{id}/actions/rebuild``.

        Destructive by definition (the disk is wiped); the platform gates it
        behind explicit confirmation + a ledger operation key. Only an image
        REFERENCE is ever sent - no credential material.
        """
        del idempotency_key
        payload = await self._request(
            "POST",
            f"/servers/{provider_server_id}/actions/rebuild",
            json={"image": image_id},
        )
        action = payload.get("action") or {}
        return str(action.get("status", "unknown"))

    async def enable_rescue(
        self,
        provider_server_id: str,
        *,
        ssh_key_ids: tuple[str, ...] | list[str] = (),
        rescue_type: str = "linux64",
    ) -> dict[str, Any]:
        """Boot into the rescue system (M13-003).

        With ``ssh_key_ids`` the provider issues NO password (key-only
        access - the protected path). Without them Hetzner generates a
        temporary root password, returned here ONCE in ``root_password``;
        the service layer wraps it as a one-time secret that never reaches
        logs, audit events or persistence.
        """
        body: dict[str, Any] = {"type": rescue_type}
        if ssh_key_ids:
            body["ssh_keys"] = list(ssh_key_ids)
        payload = await self._request(
            "POST", f"/servers/{provider_server_id}/actions/enable_rescue", json=body
        )
        action = payload.get("action") or {}
        return {
            "status": str(action.get("status", "unknown")),
            # absent/None when ssh_keys were supplied (password-free rescue)
            "root_password": payload.get("root_password"),
        }

    async def disable_rescue(self, provider_server_id: str) -> str:
        """Leave the rescue system: ``POST /servers/{id}/actions/disable_rescue``."""
        payload = await self._request(
            "POST", f"/servers/{provider_server_id}/actions/disable_rescue"
        )
        action = payload.get("action") or {}
        return str(action.get("status", "unknown"))

    async def create_snapshot(
        self,
        provider_server_id: str,
        description: str,
        idempotency_key: IdempotencyKey | None = None,
    ) -> str:
        """Create a server snapshot (M13-004): ``POST /servers/{id}/actions/create_image``.

        Returns the new image id, read from the action's resources.
        """
        del idempotency_key
        payload = await self._request(
            "POST",
            f"/servers/{provider_server_id}/actions/create_image",
            json={"type": "snapshot", "description": description},
        )
        action = payload.get("action") or {}
        for resource in action.get("resources", []):
            if resource.get("type") == "image":
                return str(resource["id"])
        raise ProviderError("create_image action returned no image resource")

    async def list_snapshots(self, provider_server_id: str | None = None) -> list[ProviderSnapshot]:
        """List available snapshots: ``GET /images?type=snapshot``.

        With ``provider_server_id`` only that server's snapshots are listed
        (``bound_to`` filter).
        """
        params: dict[str, Any] = {"type": "snapshot", "status": "available"}
        if provider_server_id is not None:
            params["bound_to"] = provider_server_id
        payload = await self._request("GET", "/images", params=params)
        result: list[ProviderSnapshot] = []
        for item in payload.get("images", []):
            created_from = item.get("created_from") or {}
            result.append(
                ProviderSnapshot(
                    id=str(item["id"]),
                    description=str(item.get("description") or ""),
                    size_gb=item.get("image_size"),
                    server_provider_id=(
                        str(created_from["id"]) if created_from.get("id") else None
                    ),
                    created_at=item.get("created"),
                )
            )
        return result

    async def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete a snapshot: ``DELETE /images/{id}``; 404 means already gone."""
        try:
            await self._request("DELETE", f"/images/{snapshot_id}")
        except ProviderNotFound:
            return

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        operation = _operation_label(method, path)
        async with metrics.provider_call(self.key, operation):
            return await self._perform_request(method, path, **kwargs)

    async def _perform_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        max_retries = self._backoff.policy.max_retries
        if self._credential_source is not None and "headers" not in kwargs:
            credential = await self._credential_source.get()
            kwargs["headers"] = self._auth_headers(credential.value)
        attempt = 0
        response: httpx.Response | None = None
        while True:
            try:
                response = await self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailable(str(exc)) from exc

            self.last_rate_limit = RateLimitSnapshot(
                _int_or_none(response.headers.get("RateLimit-Limit")),
                _int_or_none(response.headers.get("RateLimit-Remaining")),
                _int_or_none(response.headers.get("RateLimit-Reset")),
            )

            # Retry bounded times on 429, honoring the per-project reset epoch;
            # every other status is handled once below.
            if response.status_code != 429 or attempt >= max_retries:
                break
            await self._backoff.wait_before_retry(attempt, self.last_rate_limit)
            attempt += 1

        assert response is not None
        if response.status_code == 401 or response.status_code == 403:
            raise ProviderAuthError(_error_message(response))
        if response.status_code == 404:
            raise ProviderNotFound(_error_message(response))
        if response.status_code in {409, 423}:
            raise ProviderConflict(_error_message(response))
        if response.status_code == 429:
            raise ProviderRateLimited(_error_message(response), self.last_rate_limit.reset_at_unix)
        if response.status_code >= 500:
            raise ProviderUnavailable(_error_message(response))
        if response.is_error:
            raise ProviderError(_error_message(response))
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            raise ProviderError("provider returned unexpected JSON shape")
        return data

    @staticmethod
    def _map_server(item: dict[str, Any]) -> ProviderServer:
        public_net = item.get("public_net") or {}
        ipv4 = (public_net.get("ipv4") or {}).get("ip")
        ipv6 = (public_net.get("ipv6") or {}).get("ip")
        return ProviderServer(
            id=str(item["id"]),
            name=item["name"],
            status=item["status"],
            ipv4=ipv4,
            ipv6=ipv6,
            metadata={"labels": item.get("labels", {})},
        )


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _operation_label(method: str, path: str) -> str:
    """Bounded endpoint-shape label for metrics: numeric id segments become
    ``{id}`` (e.g. ``GET /servers/123/poweron`` -> ``GET /servers/{id}/poweron``)."""
    segments = path.split("?")[0].strip("/").split("/")
    shaped = ["{id}" if segment.isdigit() else segment for segment in segments]
    return f"{method} /{'/'.join(shaped)}"


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        return str(error.get("message") or error.get("code") or response.text)
    except Exception:
        return response.text or f"HTTP {response.status_code}"
