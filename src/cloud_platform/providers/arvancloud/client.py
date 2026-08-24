"""ArvanCloud IaaS transport client (M15-002).

Implements the transport half of the provider-neutral ``CloudProvider`` port
against ArvanCloud IaaS 1.0, per docs/iranian/PROVIDER_CONTRACT.md:

- Auth: ``Authorization: <MU-KEY>`` - a **plain key, no ``Bearer`` prefix**.
- Region-scoped paths under ``/regions/{region}/...``; the server ids this
  client returns are region-qualified (``{region}:{id}``) because the API
  addresses a server by (region, id) and the platform stores only the id.
- Async model: mutations are "accepted", never "finished"; ``task_state`` /
  ``status`` are exposed in metadata for the reconciliation layer.
- Idempotency is enforced platform-side (no Idempotency-Key header in the
  spec): create is guarded by a get-before-create correlation match on the
  deterministic server name; delete treats 404 as success.
- Errors map exactly onto the platform error hierarchy (§8) with the
  ``ErrorResponse {message, errors}`` shape parsed defensively.
- A conservative client-side throttle (default 10 rps, configurable) plus
  ``Retry-After`` handling on 429 (§9).
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
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.hetzner.backoff import RateLimitPolicy

#: Phase-1 advertised capabilities (PROVIDER_CONTRACT.md §3). BACKUP and
#: VOLUME are deliberately not advertised: no independent backup-management
#: API in the IaaS spec, and volume endpoints are unverified against a live
#: account.
ARVANCLOUD_CAPABILITIES = frozenset(
    {
        Capability.COMPUTE,
        Capability.POWER,
        Capability.REBUILD,
        Capability.RESCUE,
        Capability.SNAPSHOT,
        Capability.FIREWALL,
        Capability.NETWORK,
        Capability.FLOATING_IP,
        Capability.PRIMARY_IP,
        Capability.RDNS,
    }
)

#: Provider status vocabulary observed/expected so far. The exact live
#: vocabulary must be captured in docs/iranian/INTEGRATION_NOTES.md once
#: credentials exist (§12.1); unknown values pass through lower-cased.
_STATUS_MAP: dict[str, str] = {
    "available": "running",
    "running": "running",
    "started": "running",
    "active": "running",
    "creating": "building",
    "build": "building",
    "building": "building",
    "pending": "building",
    "deleting": "deleting",
    "delete": "deleting",
    "stopped": "stopped",
    "off": "stopped",
    "powered-off": "stopped",
    "error": "error",
    "failed": "error",
}


def normalize_provider_status(status: str) -> str:
    """Map a raw ArvanCloud status string to the platform state vocabulary."""
    return _STATUS_MAP.get(status.strip().lower(), status.strip().lower())


def qualify_server_id(region: str, raw_id: str) -> str:
    """``{region}:{raw_id}`` - the addressable form the platform stores."""
    return f"{region}:{raw_id}"


def parse_server_id(qualified_id: str, default_region: str | None = None) -> tuple[str, str]:
    """Split a (possibly region-qualified) server id into (region, raw_id).

    An unqualified id uses ``default_region``; that must be set or the call
    fails - the API cannot address a server without its region.
    """
    if ":" in qualified_id:
        region, raw_id = qualified_id.split(":", 1)
        if region and raw_id:
            return region, raw_id
    if default_region:
        return default_region, qualified_id
    raise ProviderError(f"cannot address server {qualified_id!r}: no region and no default region")


@dataclass(slots=False)
class Throttle:
    """A conservative client-side request throttle (max requests/second).

    ArvanCloud documents no numeric limits at capture time (§9), so the
    client enforces its own ceiling by construction. ``wait`` is injectable
    for tests (no real sleeping).
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
    """The ErrorResponse ``message`` (then the first ``errors[0]`` entry),
    parsed defensively - absent/empty bodies are allowed."""
    try:
        payload = response.json()
    except Exception:
        return response.text[:200] or f"HTTP {response.status_code}"
    if not isinstance(payload, dict):
        return response.text[:200] or f"HTTP {response.status_code}"
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()[:200]
    errors = payload.get("errors")
    if isinstance(errors, list):
        for row in errors:
            if isinstance(row, list) and row and isinstance(row[0], str):
                return row[0].strip()[:200]
            if isinstance(row, str) and row.strip():
                return row.strip()[:200]
    return f"HTTP {response.status_code}"


class ArvanCloudProvider:
    """ArvanCloud IaaS adapter (provider-neutral ``CloudProvider`` port)."""

    key = "arvancloud"
    capabilities = ARVANCLOUD_CAPABILITIES

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://napi.arvancloud.ir/ecc/v1",
        region: str = "",
        throttle: Throttle | None = None,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            # Contract §2: plain key in the Authorization header - NO Bearer prefix.
            headers={
                "Authorization": api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )
        self._region = region
        self._throttle = throttle or Throttle()
        self._max_retries = max_retries
        self._policy = RateLimitPolicy(max_retries=max_retries)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Catalog reads
    # ------------------------------------------------------------------

    async def list_locations(self) -> list[ProviderLocation]:
        """The global regions endpoint (contract §3); if the deployed spec
        version does not expose it (404), fall back to the configured region
        so the catalog can still be populated for that region."""
        try:
            payload = await self._request("GET", "/regions")
        except ProviderNotFound:
            payload = []
        if isinstance(payload, dict):
            payload = payload.get("regions", [])
        locations: list[ProviderLocation] = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            locations.append(
                ProviderLocation(
                    id=code,
                    name=str(item.get("region") or code),
                    country_code=str(item.get("country") or "IR").upper(),
                    city=item.get("city"),
                    network_zone=None,
                    metadata={
                        "dc": item.get("dc"),
                        "create_allowed": bool(item.get("create", False)),
                        "beta": bool(item.get("beta", False)),
                    },
                )
            )
        if not locations and self._region:
            locations.append(
                ProviderLocation(
                    id=self._region,
                    name=self._region,
                    country_code="IR",
                    city=None,
                    metadata={"source": "configured-default", "create_allowed": True},
                )
            )
        return locations

    async def list_plans(self) -> list[ProviderPlan]:
        region = self._require_region("list_plans")
        payload = await self._request("GET", f"/regions/{region}/sizes")
        if isinstance(payload, dict):
            payload = payload.get("plans", payload.get("sizes", []))
        plans: list[ProviderPlan] = []
        for item in payload or []:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or "").strip()
            if not raw_id:
                continue
            plans.append(
                ProviderPlan(
                    id=raw_id,
                    name=str(item.get("name") or raw_id),
                    architecture=item.get("type") or item.get("category") or "unknown",
                    vcpu=int(item.get("cpu_count") or 0),
                    memory_mb=round(float(item.get("memory_in_bytes") or 0) / (1024 * 1024))
                    or int(float(item.get("memory") or 0) * 1024),
                    disk_gb=int(item.get("disk_in_bytes") or 0) // (1024**3)
                    or int(item.get("disk") or 0),
                    # §7: provider prices pass through metadata - never hard-coded.
                    metadata={
                        "price_per_hour": item.get("price_per_hour"),
                        "price_per_day": item.get("price_per_day"),
                        "price_per_month": item.get("price_per_month"),
                        "generation": item.get("generation"),
                        "bandwidth_in_bytes": item.get("bandwidth_in_bytes"),
                        "prepaid_package_template": item.get("prepaid_package_template"),
                    },
                )
            )
        return plans

    async def list_images(self) -> list[ProviderImage]:
        region = self._require_region("list_images")
        payload = await self._request("GET", f"/regions/{region}/images")
        items = payload.get("images", []) if isinstance(payload, dict) else []
        images: list[ProviderImage] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or "").strip()
            if not raw_id:
                continue
            images.append(
                ProviderImage(
                    id=raw_id,
                    name=str(item.get("name") or raw_id),
                    os_family=str(item.get("os") or "unknown"),
                    architecture=item.get("metadata", {}).get("architecture", "unknown")
                    if isinstance(item.get("metadata"), dict)
                    else "unknown",
                    metadata={"os_version": item.get("os_version")},
                )
            )
        return images

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        try:
            region, raw_id = parse_server_id(provider_server_id, self._region)
        except ProviderError:
            return None
        try:
            payload = await self._request("GET", f"/regions/{region}/servers/{raw_id}")
        except ProviderNotFound:
            return None
        return self._map_server(region, payload)

    async def list_servers(self) -> list[ProviderServer]:
        region = self._require_region("list_servers")
        payload = await self._request("GET", f"/regions/{region}/servers")
        items = payload if isinstance(payload, list) else payload.get("servers", [])
        return [self._map_server(region, item) for item in items or [] if isinstance(item, dict)]

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer:
        """Create with a platform-side idempotency guard (§6).

        The spec documents no Idempotency-Key header, so before the mutating
        call the adapter lists the region's servers and matches the
        deterministic name from the ledger-derived request: a prior attempt
        that already materialized is returned as-is instead of creating a
        second server. ``location_id`` is the region code.
        """
        region = request.location_id
        if not region:
            raise ProviderError("create_server requires a region in request.location_id")
        existing = await self._find_by_name(region, request.name)
        if existing is not None:
            # Re-send of an already-materialized create: idempotent success.
            return existing
        body: dict[str, Any] = {
            "name": request.name,
            "flavor_id": request.plan_id,
            "image_id": request.image_id,
        }
        # ArvanCloud takes one SSH key BY NAME (§4); the platform request may
        # carry several ids - use the first and record the limitation.
        if request.ssh_key_ids:
            body["key_name"] = request.ssh_key_ids[0]
        if request.user_data is not None:
            body["init_script"] = request.user_data
        payload = await self._request("POST", f"/regions/{region}/servers", json=body)
        return self._map_server(region, payload)

    async def _find_by_name(self, region: str, name: str) -> ProviderServer | None:
        """Get-before-create correlation match (idempotency §6.2)."""
        try:
            payload = await self._request("GET", f"/regions/{region}/servers")
        except ProviderNotFound:
            return None
        items = payload if isinstance(payload, list) else payload.get("servers", [])
        for item in items or []:
            if isinstance(item, dict) and item.get("name") == name:
                return self._map_server(region, item)
        return None

    async def delete_server(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        """Delete; 404 means the resource is already gone - success (§5)."""
        del idempotency_key
        region, raw_id = parse_server_id(provider_server_id, self._region)
        try:
            await self._request("DELETE", f"/regions/{region}/servers/{raw_id}")
        except ProviderNotFound:
            return

    async def power_on(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        region, raw_id = parse_server_id(provider_server_id, self._region)
        await self._request("POST", f"/regions/{region}/servers/{raw_id}/power-on")

    async def power_off(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        region, raw_id = parse_server_id(provider_server_id, self._region)
        await self._request("POST", f"/regions/{region}/servers/{raw_id}/power-off")

    async def reboot(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        region, raw_id = parse_server_id(provider_server_id, self._region)
        await self._request("POST", f"/regions/{region}/servers/{raw_id}/reboot")

    # ------------------------------------------------------------------
    # Ambiguous-mutation probe (M15-004)
    # ------------------------------------------------------------------

    async def probe_power_effect(self, provider_server_id: str, action: str) -> bool | None:
        """Read-only probe for the platform's ambiguous power re-send guard.

        Returns whether the requested power effect ALREADY holds in steady
        state, so a timed-out mutation is never blindly re-sent:

        - ``power_on``  : True when running, False when stopped.
        - ``power_off`` : True when stopped, False when running.
        - ``reboot``    : a reboot ends in the running state, so a running
          server means the effect holds (or is indistinguishable from it);
          completing instead of re-sending avoids a second physical reboot.
          A stopped server means the reboot never applied (False).
        - transient states (building/deleting/error) and a vanished server
          are ``None`` - the platform re-queues rather than guessing.
        """
        try:
            remote = await self.get_server(provider_server_id)
        except ProviderError:
            return None
        if remote is None:
            return None
        status = remote.status
        if action == "power_on":
            if status == "running":
                return True
            if status == "stopped":
                return False
            return None
        if action == "power_off":
            if status == "stopped":
                return True
            if status == "running":
                return False
            return None
        if action == "reboot":
            if status == "running":
                return True
            if status == "stopped":
                return False
            return None
        return None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _require_region(self, operation: str) -> str:
        if not self._region:
            raise ProviderError(
                f"{operation} requires a region: configure arvancloud_region "
                "(the API addresses every resource by region)"
            )
        return self._region

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        operation = _operation_label(method, path)
        async with metrics.provider_call(self.key, operation):
            return await self._perform_request(method, path, **kwargs)

    async def _perform_request(self, method: str, path: str, **kwargs: Any) -> Any:
        attempt = 0
        while True:
            await self._throttle.acquire()
            try:
                response = await self._client.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ProviderUnavailable(str(exc)) from exc

            if response.status_code != 429 or attempt >= self._max_retries:
                break
            # §9: honor Retry-When/Retry-After when present, else back off.
            retry_after = response.headers.get("Retry-After") or response.headers.get("Retry-When")
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
        data = response.json()
        return data

    def _map_server(self, region: str, item: dict[str, Any]) -> ProviderServer:
        raw_id = str(item.get("id") or "")
        addresses = item.get("addresses")
        ipv4: str | None = None
        ipv6: str | None = None
        if isinstance(addresses, dict):
            for addr in addresses.values():
                if not isinstance(addr, list):
                    continue
                for entry in addr:
                    if not isinstance(entry, dict):
                        continue
                    version = str(entry.get("version") or "")
                    value = entry.get("addr")
                    if not value:
                        continue
                    if version == "4" and ipv4 is None:
                        ipv4 = str(value)
                    elif version == "6" and ipv6 is None:
                        ipv6 = str(value)
        status = str(item.get("status") or item.get("task_state") or "unknown")
        return ProviderServer(
            id=qualify_server_id(region, raw_id),
            name=str(item.get("name") or ""),
            status=normalize_provider_status(status),
            ipv4=ipv4,
            ipv6=ipv6,
            metadata={
                "region": region,
                "raw_status": status,
                "task_state": item.get("task_state"),
                "task_id": item.get("task_id"),
                "key_name": item.get("key_name"),
                "domain": item.get("domain"),
                "tags": item.get("tags"),
                "created": item.get("created"),
            },
        )


def _operation_label(method: str, path: str) -> str:
    """Bounded endpoint-shape label for metrics: id/region segments become
    ``{id}`` / ``{region}`` (closed label set)."""
    shaped = []
    for segment in path.split("?")[0].strip("/").split("/"):
        if segment == ":region" or segment.startswith("ir-"):
            shaped.append("{region}")
        elif segment.isdigit():
            shaped.append("{id}")
        elif len(segment) > 20:
            shaped.append("{id}")
        else:
            shaped.append(segment)
    return f"{method} /{'/'.join(shaped)}"


def _parse_retry_after(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except ValueError:
        return None
