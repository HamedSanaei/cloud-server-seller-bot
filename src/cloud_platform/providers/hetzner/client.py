from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

import httpx

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    OfferOsOption,
    OrderRecoveryResult,
    OrderRecoveryVerdict,
    ProviderImage,
    ProviderLocation,
    ProviderPlan,
    ProviderServer,
    ProviderSnapshot,
)
from cloud_platform.providers.credentials import CredentialSource
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderCapacityError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
    ProviderRateLimited,
    ProviderRejected,
    ProviderUnavailable,
)
from cloud_platform.providers.health import AccountHealth
from cloud_platform.providers.hetzner import hourly
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


class HetznerVolumeApi:
    """Block-storage volume lifecycle against the Hetzner Cloud API (M13-009).

    ``GET/POST /volumes``, ``POST /volumes/{id}/actions/attach`` and
    ``.../detach``, ``DELETE /volumes/{id}`` (404-idempotent).
    """

    def __init__(self, provider: HetznerCloudProvider) -> None:
        self._provider = provider

    async def create_volume(self, name: str, size_gb: int, location_id: str) -> tuple[str, str]:
        payload = await self._provider._request(
            "POST",
            "/volumes",
            json={"name": name, "size": int(size_gb), "location": location_id},
        )
        created = payload["volume"]
        return str(created["id"]), str(created["name"])

    async def attach_volume(self, provider_volume_id: str, provider_server_id: str) -> None:
        await self._provider._request(
            "POST",
            f"/volumes/{provider_volume_id}/actions/attach",
            json={"server": int(provider_server_id), "automount": False},
        )

    async def detach_volume(self, provider_volume_id: str) -> None:
        await self._provider._request("POST", f"/volumes/{provider_volume_id}/actions/detach")

    async def delete_volume(self, provider_volume_id: str) -> None:
        try:
            await self._provider._request("DELETE", f"/volumes/{provider_volume_id}")
        except ProviderNotFound:
            return  # already gone - deletion is idempotent

    async def list_volumes(self) -> list[tuple[str, str | None]]:
        payload = await self._provider._request("GET", "/volumes")
        out: list[tuple[str, str | None]] = []
        for item in payload.get("volumes", []):
            server = item.get("server")
            out.append((str(item["id"]), None if server is None else str(server)))
        return out


class HetznerNetworkApi:
    """Private-network lifecycle against the Hetzner Cloud API (M13-010).

    ``GET/POST /networks`` (``{"name","ip_range"}``),
    ``POST /networks/{id}/actions/attach_to_network`` and
    ``.../detach_from_network`` with ``{"server": id}``,
    ``DELETE /networks/{id}`` (404-idempotent).
    """

    def __init__(self, provider: HetznerCloudProvider) -> None:
        self._provider = provider

    async def create_network(self, name: str, ip_range: str) -> str:
        payload = await self._provider._request(
            "POST", "/networks", json={"name": name, "ip_range": ip_range}
        )
        return str(payload["network"]["id"])

    async def attach_server(self, provider_network_id: str, provider_server_id: str) -> None:
        await self._provider._request(
            "POST",
            f"/networks/{provider_network_id}/actions/attach_to_network",
            json={"server": int(provider_server_id)},
        )

    async def detach_server(self, provider_network_id: str, provider_server_id: str) -> None:
        await self._provider._request(
            "POST",
            f"/networks/{provider_network_id}/actions/detach_from_network",
            json={"server": int(provider_server_id)},
        )

    async def delete_network(self, provider_network_id: str) -> None:
        try:
            await self._provider._request("DELETE", f"/networks/{provider_network_id}")
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
        account_id: str = "default",
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
        from cloud_platform.providers.routing import normalize_account_id

        self.account_id = normalize_account_id(account_id)
        self.last_rate_limit = RateLimitSnapshot(None, None, None)
        self._backoff = RateLimitBackoff(rate_limit_policy or RateLimitPolicy())
        # M13-001/M13-006/M13-008: capability-probed management ports
        self.ssh_keys = HetznerSshKeyApi(self)
        self.firewalls = HetznerFirewallApi(self)
        self.floating_ips = HetznerFloatingIpApi(self)
        self.volumes = HetznerVolumeApi(self)
        self.networks = HetznerNetworkApi(self)
        # VPS ports share this credential holder and its HTTP client.
        from cloud_platform.providers.hetzner.vps import HetznerVpsManagement

        self.vps_management = HetznerVpsManagement(self)

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
            await self._request("GET", "/locations")
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
            "GET", "/locations", headers=self._auth_headers(candidate)
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
        items = await self._pages("/images", "images", {"type": "system"})
        return [
            ProviderImage(
                id=str(item["id"]),
                name=item.get("name") or str(item["id"]),
                os_family="linux",
                architecture=item.get("architecture", "unknown"),
                metadata={"os_version": item.get("os_version"), "os_flavor": item.get("os_flavor")},
            )
            for item in items
            if item.get("type") == "system"
            and item.get("status") == "available"
            and not item.get("deprecated")
            and item.get("os_flavor")
            in {
                "ubuntu",
                "debian",
                "centos",
                "fedora",
                "rocky",
                "alma",
            }
        ]

    async def validate_create_request(self, request: CreateServerRequest) -> None:
        """Account-native proof for the frozen legacy compute request, READ-ONLY."""
        locations = await self._pages("/locations", "locations")
        location = next(
            (
                item
                for item in locations
                if request.location_id in {str(item.get("id")), item.get("name")}
            ),
            None,
        )
        if location is None or not isinstance(location.get("name"), str):
            raise ProviderNotFound("requested location is not available to this account")
        location_name = location["name"]
        items = await self._pages(
            "/server_types",
            "server_types",
            {"location": location_name},
        )
        plan = next(
            (item for item in items if request.plan_id in {str(item.get("id")), item.get("name")}),
            None,
        )
        if (
            plan is None
            or plan.get("deprecated")
            or plan.get("deprecation")
            or hourly._location_availability(plan, location_name) is not True
        ):
            raise ProviderNotFound("requested product/location membership is not proven")
        images = await self.list_images()
        image = next(
            (image for image in images if request.image_id in {image.id, image.name}), None
        )
        if image is None or image.architecture != plan.get("architecture"):
            raise ProviderNotFound("frozen image is unavailable or incompatible on this account")

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
            "labels": {
                **request.labels,
                "platform-operation": operation_label(idempotency_key.value),
            },
        }
        if request.user_data is not None:
            body["user_data"] = request.user_data
        payload = await self._mutation_request("POST", "/servers", json=body)
        item = payload.get("server")
        if not isinstance(item, dict) or not _created_server_matches(item, body):
            raise ProviderOutcomeUnknown("Hetzner create response identity not proven")
        try:
            return self._map_server(item)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ProviderOutcomeUnknown("Hetzner create returned an incomplete server") from exc

    # --- Offer options / checkout revalidation (provider-neutral port) ---

    async def get_pricing_currency(self) -> str:
        """Read the pinned Project owner's currency from the official price envelope."""
        return hourly.pricing_currency(await self._request("GET", "/pricing"))

    async def _server_type_at_location(
        self, location_id: str, product_id: str
    ) -> dict[str, Any] | None:
        """The LIST entry for one server type at one location, or None.

        The location-scoped LIST endpoint is authoritative: the per-id detail
        endpoint is not required (and is not used) here.
        """
        entries = await self._pages(
            "/server_types",
            "server_types",
            {"name": product_id, "location": location_id},
        )
        matches = [
            raw
            for raw in entries
            if str(raw.get("name")) == product_id or str(raw.get("id")) == product_id
        ]
        if len(matches) > 1:
            raise ProviderError("server type identity is ambiguous")
        if matches:
            return matches[0]
        return None

    @staticmethod
    def _monthly_minor_for_location(
        item: dict[str, Any], location_id: str, *, currency: str
    ) -> int | None:
        """Use the catalog's authoritative location and price parser."""
        raw, _reason = hourly._location_price_entry(item, location_id)
        if raw is None or hourly._location_availability(item, location_id) is not True:
            return None
        value, _reason = hourly._gross_decimal(raw, hourly.MONTHLY_PRICE_KEY)
        return hourly.minor_units(value, currency) if value is not None else None

    async def get_os_options(self, location_id: str, product_id: str) -> list[OfferOsOption]:
        """Selectable system images for one server type at one location.

        Only images the provider currently offers for CREATION are exposed
        (``type=system``, deprecated images excluded), filtered to the server
        type's architecture so an incompatible image cannot be selected.
        """
        plan = await self._server_type_at_location(location_id, product_id)
        if (
            plan is None
            or plan.get("deprecated")
            or plan.get("deprecation")
            or hourly._location_availability(plan, location_id) is not True
            or plan.get("architecture") not in {"x86", "arm"}
        ):
            return []
        images = await self.list_images()
        options = [
            OfferOsOption(name=image.name, image_id=image.id)
            for image in images
            if image.architecture == plan["architecture"]
        ]
        return sorted(options, key=lambda option: option.name)

    async def validate_offer_for_checkout(
        self,
        *,
        location_id: str,
        product_id: str,
        os_name: str,
        expected_cost_minor: int,
        currency: str,
    ) -> None:
        """READ-ONLY revalidation immediately before a billable create.

        Raises when the provider no longer offers the server type at the
        location, when its monthly price/currency moved away from the
        catalog snapshot this order was priced from, or when the selected OS
        image is no longer a creatable system image.
        """
        item = await self._server_type_at_location(location_id, product_id)
        if item is None:
            raise ProviderNotFound(f"server type {product_id} is not offered at {location_id}")
        observed_currency = await self.get_pricing_currency()
        if observed_currency != currency:
            raise ProviderConflict("provider currency differs from the accepted native cost")
        observed = self._monthly_minor_for_location(item, location_id, currency=observed_currency)
        if observed is None:
            raise ProviderNotFound(
                f"server type {product_id} has no monthly price at {location_id}"
            )
        if observed != expected_cost_minor:
            raise ProviderConflict(
                f"provider price changed for {product_id} at {location_id} since catalog sync"
            )
        if not await self._is_creatable_image(os_name, item.get("architecture")):
            raise ProviderNotFound(f"image {os_name!r} is no longer available for creation")

    async def _is_creatable_image(self, os_name: str, architecture: str | None) -> bool:
        """Whether ``os_name`` still resolves to a creatable system image.

        Matches by image id first, then by name: the platform persists the
        stable provider reference it was given, and the provider accepts
        either form for creation.
        """
        images = await self.list_images()
        return any(
            os_name in {image.id, image.name} and image.architecture == architecture
            for image in images
        )

    # --- Read-only recovery for an ambiguous direct create ---

    async def recover_server_by_operation(
        self,
        operation_key: str,
        since: datetime | None = None,
        *,
        legacy_label: bool = True,
        platform_server_id: str | None = None,
    ) -> OrderRecoveryResult:
        """Exact correlation only; complete absence is not permission to re-POST."""
        del since
        label = operation_key[:63] if legacy_label else operation_label(operation_key)
        try:
            inventory = await self._pages("/servers", "servers")
        except ProviderError as exc:
            return OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.SCAN_FAILED, reason=type(exc).__name__
            )
        matches = []
        conflict = False
        for item in inventory:
            labels = item.get("labels")
            if not isinstance(labels, dict) or labels.get("platform-operation") != label:
                continue
            matches.append(item)
            if (
                platform_server_id is not None
                and "platform_server_id" in labels
                and labels["platform_server_id"] != platform_server_id
            ):
                conflict = True
        if not matches:
            return OrderRecoveryResult(verdict=OrderRecoveryVerdict.NO_MATCH)
        if len(matches) != 1 or conflict:
            return OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.AMBIGUOUS,
                candidate_count=len(matches),
                reason="conflicting operation correlation",
            )
        return OrderRecoveryResult(
            verdict=OrderRecoveryVerdict.MATCHED,
            provider_order_id=str(matches[0]["id"]),
            candidate_count=1,
        )

    async def delete_server(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        # This API has no DELETE idempotency key. Prove absence before issuing
        # a mutation, especially when the operation is re-entered after a
        # timeout or a worker restart.
        if await self.get_server(provider_server_id) is None:
            return
        try:
            await self._mutation_request("DELETE", f"/servers/{provider_server_id}")
        except ProviderNotFound:
            return

    async def power_on(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self.vps_management.start_vps(provider_server_id)

    async def power_off(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self.vps_management.stop_vps(provider_server_id)

    async def reboot(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self.vps_management.reboot_vps(provider_server_id)

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
        payload = await self._mutation_request(
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
        payload = await self._mutation_request(
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

    async def _mutation_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """One mutation, never retried; ambiguous acceptance is surfaced to callers."""
        return await self._request(method, path, _mutation=True, **kwargs)

    async def _perform_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        mutation = kwargs.pop("_mutation", False)
        max_retries = (
            self._backoff.policy.max_retries if method.upper() == "GET" and not mutation else 0
        )
        if self._credential_source is not None and "headers" not in kwargs:
            credential = await self._credential_source.get()
            kwargs["headers"] = self._auth_headers(credential.value)
        attempt = 0
        response: httpx.Response | None = None
        while True:
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                if mutation:
                    raise ProviderOutcomeUnknown(
                        "Hetzner mutation transport outcome unknown"
                    ) from exc
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
        capacity = _capacity_error(response)
        if capacity is not None:
            raise capacity
        if mutation and response.status_code == 408:
            raise ProviderOutcomeUnknown("Hetzner mutation outcome unknown (HTTP 408)")
        if response.status_code == 401 or response.status_code == 403:
            raise ProviderAuthError(_error_message(response))
        if response.status_code == 404:
            raise ProviderNotFound(_error_message(response))
        if response.status_code in {409, 423}:
            raise ProviderConflict(_error_message(response))
        if response.status_code == 429:
            if mutation:
                raise ProviderOutcomeUnknown("Hetzner mutation outcome unknown (HTTP 429)")
            raise ProviderRateLimited(_error_message(response), self.last_rate_limit.reset_at_unix)
        if response.status_code >= 500:
            if mutation:
                raise ProviderOutcomeUnknown(
                    f"Hetzner mutation outcome unknown (HTTP {response.status_code})"
                )
            raise ProviderUnavailable(_error_message(response))
        if response.is_error:
            if mutation and response.status_code in {400, 412, 422}:
                code = _error_code(response)
                if (
                    (response.status_code == 400 and code == "json_error")
                    or (response.status_code == 412 and code == "resource_unavailable")
                    or (
                        response.status_code == 422
                        and code in {"invalid_input", "unsupported_error"}
                    )
                ):
                    raise ProviderRejected("Hetzner rejected the mutation request")
                raise ProviderOutcomeUnknown("Hetzner mutation has no definitive rejection proof")
            raise ProviderError(_error_message(response))
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            if mutation:
                raise ProviderOutcomeUnknown("Hetzner mutation returned invalid JSON") from exc
            raise ProviderError("provider returned invalid JSON") from exc
        if not isinstance(data, dict):
            if mutation:
                raise ProviderOutcomeUnknown("Hetzner mutation returned unexpected JSON shape")
            raise ProviderError("provider returned unexpected JSON shape")
        return data

    def _map_server(self, item: dict[str, Any]) -> ProviderServer:
        public_net = item.get("public_net") or {}
        ipv4 = (public_net.get("ipv4") or {}).get("ip")
        ipv6 = (public_net.get("ipv6") or {}).get("ip")
        return ProviderServer(
            id=str(item["id"]),
            name=item["name"],
            status=item["status"],
            ipv4=ipv4,
            ipv6=ipv6,
            metadata={
                "labels": item.get("labels", {}),
                "credential_account_id": self.account_id,
            },
        )

    async def project_server_count(self) -> int:
        """Count every existing server in this credential's Project, not its owner account."""
        return len(await self._pages("/servers", "servers"))

    async def _pages(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Strict complete inventory; partial reads never establish capacity or absence."""
        items: list[dict[str, Any]] = []
        ids: set[str] = set()
        page = 1
        total: int | None = None
        last_page: int | None = None
        for _ in range(1000):
            payload = await self._request(
                "GET", path, params={**(params or {}), "page": page, "per_page": 50}
            )
            raw = payload.get(key)
            meta = payload.get("meta")
            pagination = meta.get("pagination") if isinstance(meta, dict) else None
            if not isinstance(raw, list) or not isinstance(pagination, dict):
                raise ProviderError(f"{path} returned an incomplete list envelope")
            if (
                "next_page" not in pagination
                or type(pagination.get("page")) is not int
                or pagination["page"] != page
            ):
                raise ProviderError(f"{path} returned invalid pagination")
            for field in ("total_entries", "last_page"):
                value = pagination.get(field)
                if value is None:
                    continue
                if type(value) is not int or value < (1 if field == "last_page" else 0):
                    raise ProviderError(f"{path} returned invalid pagination")
                previous = total if field == "total_entries" else last_page
                if previous is not None and value != previous:
                    raise ProviderError(f"{path} returned inconsistent pagination")
                if field == "total_entries":
                    total = value
                else:
                    last_page = value
            for item in raw:
                if not isinstance(item, dict):
                    raise ProviderError(f"{path} returned an invalid list entry")
                if key == "servers":
                    identity = _resource_id(item.get("id"))
                    if identity is None or identity in ids:
                        raise ProviderError(f"{path} returned invalid or duplicate server identity")
                    ids.add(identity)
                items.append(item)
            next_page = pagination["next_page"]
            if next_page is None:
                if (last_page is not None and last_page != page) or (
                    total is not None and total != len(items)
                ):
                    raise ProviderError(f"{path} returned incomplete pagination")
                return items
            if (
                type(next_page) is not int
                or next_page != page + 1
                or (last_page is not None and next_page > last_page)
            ):
                raise ProviderError(f"{path} returned invalid pagination")
            page = next_page
        raise ProviderError(f"{path} exceeded safe pagination limit")


def operation_label(operation_key: str) -> str:
    """Documented label-safe exact operation correlation for new creates."""
    return "op-" + sha256(operation_key.encode("utf-8")).hexdigest()[:60]


def _resource_id(value: Any) -> str | None:
    if type(value) is int and value > 0:
        return str(value)
    if (
        isinstance(value, str)
        and 0 < len(value) <= 20
        and value.isascii()
        and value.isdigit()
        and not value.startswith("0")
        and int(value) > 0
    ):
        return value
    return None


def _identity_matches(value: Any, expected: str) -> bool:
    if isinstance(value, dict):
        return any(str(value.get(key)) == expected for key in ("id", "name"))
    return isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) == expected


def _created_server_matches(item: Any, body: dict[str, Any]) -> bool:
    if not isinstance(item, dict) or _resource_id(item.get("id")) is None:
        return False
    if (
        item.get("name") != body["name"]
        or not isinstance(item.get("status"), str)
        or not item["status"]
    ):
        return False
    if not _identity_matches(item.get("server_type"), body["server_type"]):
        return False
    if not _identity_matches(item.get("image"), body["image"]):
        return False
    datacenter = item.get("datacenter")
    nested = datacenter.get("location") if isinstance(datacenter, dict) else None
    direct = item.get("location")
    if nested is None and direct is None:
        return False
    if any(
        value is not None and not _identity_matches(value, body["location"])
        for value in (nested, direct)
    ):
        return False
    labels = item.get("labels")
    return isinstance(labels, dict) and all(
        labels.get(key) == value for key, value in body["labels"].items()
    )


def _capacity_error(response: httpx.Response) -> ProviderCapacityError | None:
    if response.status_code != 403:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) or error.get("code") != "resource_limit_exceeded":
        return None
    details = error.get("details")
    limits = details.get("limits") if isinstance(details, dict) else None
    names: list[str] = []
    if isinstance(limits, list):
        for limit in limits:
            name = limit.get("name") if isinstance(limit, dict) else None
            if (
                isinstance(name, str)
                and 0 < len(name) <= 128
                and name.isascii()
                and all(character.isalnum() or character in "_-." for character in name)
                and name not in names
            ):
                names.append(name)
    return ProviderCapacityError(
        "Hetzner Project resource limit exceeded",
        error_code="resource_limit_exceeded",
        quota_names=tuple(names),
        definitive_refusal=True,
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


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        return str(error.get("message") or error.get("code") or response.text)
    except Exception:
        return response.text or f"HTTP {response.status_code}"
