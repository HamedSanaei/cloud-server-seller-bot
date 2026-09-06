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
- A conservative client-side throttle (default 10 rps, configurable) plus
  ``Retry-After`` handling on 429.
- The key never appears in errors, logs, or metric labels; error text passes
  through the shared redaction discipline.
"""

from __future__ import annotations

import asyncio
import time
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


@dataclass(slots=False)
class Throttle:
    """A conservative client-side request throttle (max requests/second).

    Leaseweb documents no numeric limits at capture time, so the client
    enforces its own ceiling by construction. ``wait`` is injectable for
    tests (no real sleeping).
    """

    max_rps: float = 10.0
    wait: Any = asyncio.sleep

    def __post_init__(self) -> None:
        if self.max_rps <= 0:
            raise ValueError("max_rps must be > 0")
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, now: Any = time.monotonic) -> None:
        async with self._lock:
            interval = 1.0 / self.max_rps
            wait_time = self._last + interval - now()
            if wait_time > 0:
                await self.wait(wait_time)
            self._last = now()


def _error_payload(response: httpx.Response) -> str:
    """The ``errorMessage`` (then ``errorCode``), parsed defensively."""
    try:
        payload = response.json()
    except Exception:
        return response.text[:200] or f"HTTP {response.status_code}"
    if not isinstance(payload, dict):
        return response.text[:200] or f"HTTP {response.status_code}"
    message = payload.get("errorMessage")
    if isinstance(message, str) and message.strip():
        return message.strip()[:200]
    code = payload.get("errorCode")
    if code is not None and str(code).strip():
        return f"leaseweb error {code}".strip()[:200]
    errors = payload.get("errors")
    if isinstance(errors, list):
        for row in errors:
            if isinstance(row, str) and row.strip():
                return row.strip()[:200]
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


class LeaseWebProvider:
    """Leaseweb Public Cloud adapter (provider-neutral ``CloudProvider`` port)."""

    key = "leaseweb"
    capabilities = LEASEWEBCLOUD_CAPABILITIES

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.leaseweb.com",
        region: str = "",
        throttle: Throttle | None = None,
        max_retries: int = 3,
        credential_source: CredentialSource | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if credential_source is not None:
            # Runtime-rotatable (M10-008): the auth header is set per
            # request from the source; api_key is only the initial value.
            default_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        else:
            # Contract: plain key in the X-LSW-Auth header - NO Bearer prefix.
            default_headers = {
                "X-LSW-Auth": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=default_headers,
            timeout=httpx.Timeout(30.0),
        )
        self._credential_source = credential_source
        self._region = region
        self._throttle = throttle or Throttle()
        self._max_retries = max_retries

    async def close(self) -> None:
        await self._client.aclose()

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
        response = await self._client.request(
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
        operation = _operation_label(method, path)
        async with metrics.provider_call(self.key, operation):
            return await self._perform_request(method, path, **kwargs)

    async def _perform_request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._credential_source is not None and "headers" not in kwargs:
            credential = await self._credential_source.get()
            kwargs["headers"] = {"X-LSW-Auth": credential.value}
        attempt = 0
        while True:
            await self._throttle.acquire()
            try:
                response = await self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailable(str(exc)) from exc

            if response.status_code != 429 or attempt >= self._max_retries:
                break
            retry_after = response.headers.get("Retry-After")
            delay = _parse_retry_after(retry_after)
            if delay is None:
                delay = 0.5 * (2**attempt)
            await self._throttle.wait(min(delay, 30.0))
            attempt += 1

        assert response is not None
        return self._raise_for_status(response)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> Any:
        message = _error_payload(response)
        if response.status_code in (401, 403):
            raise ProviderAuthError(message)
        if response.status_code == 404:
            raise ProviderNotFound(message)
        if response.status_code in (409, 423) or "already" in message.lower():
            raise ProviderConflict(message)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            raise ProviderRateLimited(message, _parse_retry_after(retry_after))
        if response.status_code >= 500:
            raise ProviderUnavailable(message)
        if response.is_error:
            raise ProviderError(message)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

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


def _operation_label(method: str, path: str) -> str:
    """Bounded endpoint-shape label for metrics: UUID segments become ``{id}``."""
    shaped = []
    for segment in path.split("?")[0].strip("/").split("/"):
        if len(segment) > 20 or _looks_like_uuid(segment):
            shaped.append("{id}")
        else:
            shaped.append(segment)
    return f"{method} /{'/'.join(shaped)}"


def _looks_like_uuid(segment: str) -> bool:
    parts = segment.split("-")
    hexdigits = set("0123456789abcdefABCDEF")
    return len(parts) == 5 and all(p and all(c in hexdigits for c in p) for p in parts)


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except ValueError:
        return None


# Back-compat alias: both spellings resolve to the same adapter so callers
# (container, worker, tests) never break on a rename either way.
LeasewebCloudProvider = LeaseWebProvider
