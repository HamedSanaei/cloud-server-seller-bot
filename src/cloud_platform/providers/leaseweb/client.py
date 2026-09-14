"""Leaseweb Public Cloud transport client (M17-001).

Implements the provider-neutral ``CloudProvider`` port against Leaseweb's
Public Cloud v1 API (``https://api.leaseweb.com``), per
``docs/leaseweb/INTEGRATION_NOTES.md``:

- Auth: ``X-LSW-Auth: <API-KEY>`` request header (no ``Bearer`` prefix).
- Region-scoped reads under ``/publicCloud/v1``; instance ids are used as
  returned by the API (UUIDs) and stored verbatim.
- Async model: launch is "accepted", never "finished"; ``state`` is exposed
  in metadata for the reconciliation layer.
- Idempotency is enforced platform-side (no Idempotency-Key header in the
  public API): create is guarded by a get-before-create correlation match on
  the deterministic server name; delete treats 404 as success.
- Errors map onto the platform error hierarchy with the
  ``{errorCode, errorMessage, correlationId}`` shape parsed defensively.
- The HTTP transport is the ONE shared
  :class:`~cloud_platform.providers.leaseweb.transport.LeasewebTransport`
  (LEASEWEB-VPS-API §5): it owns the base URL, auth header, timeouts,
  429/``Retry-After`` handling, structured error mapping, redaction and the
  conservative client-side throttle (default 10 rps, configurable).
- The key never appears in errors, logs, or metric labels; error text passes
  through the shared redaction discipline.

The mapping from Public Cloud JSON to ``ProviderServer`` stays deliberately
tolerant (dict-based): this is a provider-neutral read path that must keep
working across provider schema variance. The strict, typed Leaseweb contract
lives in the ordering/orders/VPS API clients.
"""

from __future__ import annotations

from typing import Any

import httpx

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    ProviderImage,
    ProviderLocation,
    ProviderPlan,
    ProviderServer,
)
from cloud_platform.providers.credentials import CredentialSource
from cloud_platform.providers.errors import ProviderError, ProviderNotFound
from cloud_platform.providers.leaseweb.errors import (
    error_for_response,
    parse_error_payload,
    redact_sensitive,
)
from cloud_platform.providers.leaseweb.transport import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    LeasewebTransport,
    Throttle,
    operation_label,
    parse_retry_after,
)

__all__ = [
    "LEASEWEBCLOUD_CAPABILITIES",
    "LeaseWebProvider",
    "LeasewebCloudProvider",
    "Throttle",
    "normalize_provider_status",
]

#: Phase-1 advertised capabilities (docs/leaseweb/INTEGRATION_NOTES.md).
#: COMPUTE + POWER is the sellable minimum (create/delete/power + catalog +
#: billing + bot). Extra capabilities stay disabled until a live account
#: confirms the exact endpoint shapes (same discipline as ArvanCloud M15).
LEASEWEBCLOUD_CAPABILITIES = frozenset(
    {
        Capability.COMPUTE,
        Capability.POWER,
    }
)

#: Raw ``state`` vocabulary observed in Leaseweb samples (``RUNNING`` is
#: documented in the launch-instance response). Unknown values pass through
#: lower-cased so the reconciler contains instead of misclassifying.
_STATUS_MAP: dict[str, str] = {
    "running": "running",
    "started": "running",
    "active": "running",
    "available": "running",
    "creating": "building",
    "build": "building",
    "building": "building",
    "pending": "building",
    "provisioning": "building",
    "stopping": "stopping",
    "stopped": "stopped",
    "off": "stopped",
    "powered-off": "stopped",
    "starting": "building",
    "rebooting": "building",
    "terminating": "deleting",
    "deleting": "deleting",
    "terminated": "deleted",
    "deleted": "deleted",
    "error": "error",
    "failed": "error",
}


def normalize_provider_status(status: str) -> str:
    """Map a raw Leaseweb state string to the platform state vocabulary."""
    return _STATUS_MAP.get(status.strip().lower(), status.strip().lower())


def _error_payload(response: httpx.Response) -> str:
    """The ``errorMessage`` (then ``errorCode``), parsed defensively.

    Legacy public entry point kept for back-compat (imported by other
    Leaseweb modules and tests). The text is scrubbed through the shared
    redaction discipline: a provider payload may echo a request body.
    """
    try:
        payload = response.json()
    except Exception:
        text = redact_sensitive((response.text or "").strip())[:200]
        return text or f"HTTP {response.status_code}"
    if not isinstance(payload, dict):
        text = redact_sensitive((response.text or "").strip())[:200]
        return text or f"HTTP {response.status_code}"
    message = payload.get("errorMessage")
    if isinstance(message, str) and message.strip():
        return redact_sensitive(message.strip())[:200]
    code = payload.get("errorCode")
    if code is not None and str(code).strip():
        return f"leaseweb error {code}".strip()[:200]
    errors = payload.get("errors")
    if isinstance(errors, list):
        for row in errors:
            if isinstance(row, str) and row.strip():
                return redact_sensitive(row.strip())[:200]
    return f"HTTP {response.status_code}"


def _as_list(payload: Any, *keys: str) -> list[Any]:
    """Extract a list from a Leaseweb envelope (bare list or keyed dict)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            items = payload.get(key)
            if isinstance(items, list):
                return items
    return []


def _operation_label(method: str, path: str) -> str:
    """Bounded endpoint-shape label for metrics (see the transport)."""
    return operation_label(method, path)


def _parse_retry_after(value: str | None) -> int | None:
    """Parse a ``Retry-After`` header (back-compat alias)."""
    return parse_retry_after(value)


class LeaseWebProvider:
    """Leaseweb Public Cloud adapter (provider-neutral ``CloudProvider`` port)."""

    key = "leaseweb"
    capabilities = LEASEWEBCLOUD_CAPABILITIES

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        region: str = "",
        throttle: Throttle | None = None,
        max_retries: int = 3,
        credential_source: CredentialSource | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = LeasewebTransport(
            api_key,
            base_url,
            timeout_seconds=timeout_seconds,
            throttle=throttle,
            max_retries=max_retries,
            credential_source=credential_source,
            provider_key=self.key,
        )
        self._credential_source = credential_source
        self._region = region
        self._throttle = self._transport.throttle
        self._max_retries = max_retries

    @property
    def _client(self) -> httpx.AsyncClient:
        """The ONE transport's HTTP client (patched by tests)."""
        return self._transport.client

    @_client.setter
    def _client(self, client: httpx.AsyncClient) -> None:
        self._transport.set_client(client)

    async def close(self) -> None:
        await self._transport.aclose()

    # ------------------------------------------------------------------
    # Catalog reads
    # ------------------------------------------------------------------

    async def list_locations(self) -> list[ProviderLocation]:
        """List Public Cloud regions (``GET /publicCloud/v1/regions``).

        When the endpoint is unavailable the configured default region is
        returned so catalog sync can still be populated for that region.
        """
        try:
            payload = await self._request("GET", "/publicCloud/v1/regions")
        except ProviderNotFound:
            payload = []
        locations: list[ProviderLocation] = []
        for item in _as_list(payload, "regions", "data", "items"):
            if not isinstance(item, dict):
                continue
            code = str(item.get("name") or item.get("id") or item.get("code") or "").strip()
            if not code:
                continue
            country = str(item.get("country") or item.get("countryCode") or "NL").upper()
            locations.append(
                ProviderLocation(
                    id=code,
                    name=str(item.get("displayName") or item.get("name") or code),
                    country_code=country,
                    city=item.get("city"),
                    network_zone=None,
                    metadata={"source": "leaseweb-regions"},
                )
            )
        if not locations and self._region:
            locations.append(
                ProviderLocation(
                    id=self._region,
                    name=self._region,
                    country_code="NL",
                    city=None,
                    metadata={"source": "configured-default"},
                )
            )
        return locations

    async def list_plans(self) -> list[ProviderPlan]:
        """List instance types for the configured region.

        Prices (when the API exposes them) pass through ``metadata`` - never
        hard-coded (same discipline as the ArvanCloud adapter).
        """
        params: dict[str, Any] = {}
        if self._region:
            params["region"] = self._region
        payload = await self._request("GET", "/publicCloud/v1/instanceTypes", params=params)
        plans: list[ProviderPlan] = []
        for item in _as_list(payload, "instanceTypes", "types", "data", "items"):
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("name") or item.get("id") or "").strip()
            if not raw_id:
                continue
            raw = item.get("resources")
            resources: dict[str, Any] = raw if isinstance(raw, dict) else {}
            vcpu = int(item.get("cpu") or item.get("vcpus") or resources.get("cpu") or 0)
            memory_mb = _memory_mb(item, resources)
            disk_gb = int(
                item.get("rootDiskSize") or resources.get("disk") or item.get("disk") or 0
            )
            plans.append(
                ProviderPlan(
                    id=raw_id,
                    name=str(item.get("displayName") or raw_id),
                    architecture=str(item.get("architecture") or "x86_64"),
                    vcpu=vcpu,
                    memory_mb=memory_mb,
                    disk_gb=disk_gb,
                    metadata={
                        "price_per_hour": item.get("pricePerHour", item.get("price_per_hour")),
                        "price_per_month": item.get("pricePerMonth", item.get("price_per_month")),
                        "region": self._region,
                    },
                )
            )
        return plans

    async def list_images(self) -> list[ProviderImage]:
        params: dict[str, Any] = {}
        if self._region:
            params["region"] = self._region
        payload = await self._request("GET", "/publicCloud/v1/images", params=params)
        images: list[ProviderImage] = []
        for item in _as_list(payload, "images", "data", "items"):
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or item.get("name") or "").strip()
            if not raw_id:
                continue
            images.append(
                ProviderImage(
                    id=raw_id,
                    name=str(item.get("displayName") or item.get("name") or raw_id),
                    os_family=str(item.get("os") or item.get("family") or "unknown"),
                    architecture=str(item.get("architecture") or "x86_64"),
                    metadata={"os_version": item.get("version")},
                )
            )
        return images

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        try:
            payload = await self._request("GET", f"/publicCloud/v1/instances/{provider_server_id}")
        except ProviderNotFound:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("instance"), dict):
            payload = payload["instance"]
        if not isinstance(payload, dict):
            return None
        return self._map_server(payload)

    async def list_servers(self) -> list[ProviderServer]:
        params: dict[str, Any] = {"limit": 100, "offset": 0}
        if self._region:
            params["region"] = self._region
        servers: list[ProviderServer] = []
        while True:
            payload = await self._request("GET", "/publicCloud/v1/instances", params=params)
            items = _as_list(payload, "instances", "data", "items")
            servers.extend(self._map_server(item) for item in items if isinstance(item, dict))
            meta = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
            total = meta.get("totalCount")
            if not isinstance(total, int) or len(servers) >= total or not items:
                return servers
            params["offset"] = int(params["offset"]) + len(items)

    async def verify_credential(self, candidate: str) -> None:
        """Verify a CANDIDATE key with a read-only call (M10-008).

        The candidate is sent only for this one request; the live key (if
        any) is untouched.
        """
        response = await self._transport.request_raw(
            "GET", "/publicCloud/v1/regions", headers={"X-LSW-Auth": candidate}
        )
        self._raise_for_status(response)

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer:
        """Launch an instance with a platform-side idempotency guard.

        The public API documents no Idempotency-Key header, so before the
        mutating call the adapter lists the region's instances and matches
        the deterministic name from the ledger-derived request: a prior
        attempt that already materialized is returned as-is instead of
        launching a second instance.
        """
        existing = await self._find_by_name(request.name)
        if existing is not None:
            # Re-send of an already-materialized create: idempotent success.
            return existing
        body: dict[str, Any] = {
            "type": request.plan_id,
            "imageId": request.image_id,
            "region": request.location_id or self._region,
            "reference": request.name[:64],
        }
        if request.ssh_key_ids:
            body["sshKey"] = request.ssh_key_ids[0]
        if request.user_data is not None:
            body["userData"] = request.user_data
        # Correlation label so the orphan detector can attribute the resource.
        labels = dict(request.labels)
        labels.setdefault("platform-operation", idempotency_key.value[:63])
        body["labels"] = labels
        payload = await self._request("POST", "/publicCloud/v1/instances", json=body)
        if isinstance(payload, dict) and isinstance(payload.get("instance"), dict):
            payload = payload["instance"]
        if not isinstance(payload, dict):
            raise ProviderError("launch instance returned an unexpected payload")
        return self._map_server(payload)

    async def _find_by_name(self, name: str) -> ProviderServer | None:
        """Get-before-create correlation match (idempotency)."""
        try:
            servers = await self.list_servers()
        except ProviderNotFound:
            return None
        for server in servers:
            if server.name == name:
                return server
        return None

    async def delete_server(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        """Terminate; 404 means the resource is already gone - success."""
        del idempotency_key
        try:
            await self._request("DELETE", f"/publicCloud/v1/instances/{provider_server_id}")
        except ProviderNotFound:
            return

    async def power_on(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/publicCloud/v1/instances/{provider_server_id}/start")

    async def power_off(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/publicCloud/v1/instances/{provider_server_id}/stop")

    async def reboot(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/publicCloud/v1/instances/{provider_server_id}/reboot")

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform one request through the shared Leaseweb transport.

        Public Cloud calls keep the historical read classification (this
        adapter's create path is guarded by a get-before-create match on a
        client-chosen name, so a transport failure stays retryable).
        """
        return await self._transport.request(method, path, **kwargs)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> Any:
        """Map a raw response onto the Leaseweb error hierarchy (read path)."""
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        payload = parse_error_payload(response)
        raise error_for_response(payload)

    def _map_server(self, item: dict[str, Any]) -> ProviderServer:
        raw_id = str(item.get("id") or "")
        ips = item.get("ips") or item.get("ipAddresses") or []
        ipv4: str | None = None
        ipv6: str | None = None
        if isinstance(ips, list):
            for entry in ips:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("ip") or entry.get("address")
                if not value:
                    continue
                family = str(entry.get("version") or entry.get("type") or "")
                if ("6" in family or ":" in str(value)) and ipv6 is None:
                    ipv6 = str(value)
                elif ipv4 is None:
                    ipv4 = str(value)
        else:
            public = item.get("publicIp") or item.get("ip")
            if public:
                ipv4 = str(public)
        status = str(item.get("state") or item.get("status") or "unknown")
        return ProviderServer(
            id=raw_id,
            name=str(item.get("reference") or item.get("name") or raw_id),
            status=normalize_provider_status(status),
            ipv4=ipv4,
            ipv6=ipv6,
            metadata={
                "region": item.get("region", self._region),
                "raw_state": status,
                "type": item.get("type"),
                "contract": item.get("contract"),
                "contract_ends_at": item.get("contractEndAt"),
            },
        )


def _memory_mb(item: dict[str, Any], resources: dict[str, Any]) -> int:
    for key in ("memoryMb", "memoryMB", "memory_mb", "ram"):
        value = item.get(key, resources.get(key))
        if value is not None:
            try:
                return int(float(str(value)))
            except ValueError:
                continue
    for key in ("memoryGb", "memoryGB", "memory_gb"):
        value = item.get(key, resources.get(key))
        if value is not None:
            try:
                return int(float(str(value)) * 1024)
            except ValueError:
                continue
    return 0


# Back-compat alias: both spellings resolve to the same adapter so callers
# (container, worker, tests) never break on a rename either way.
LeasewebCloudProvider = LeaseWebProvider
