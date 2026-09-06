"""Leaseweb Ordering API v1 adapter (LEASEWEB-MVP).

Implements the provider-neutral ``CloudProvider`` read port plus the
optional :class:`~cloud_platform.providers.base.OrderingProvider` port
against Leaseweb's **ordering VPS** product family, per the official
OpenAPI specs (``github.com/Leaseweb/api-definitions``: ``ordering/``,
``orders/``, ``vps/``) and ``docs/leaseweb/MVP_DESIGN.md``:

- Auth: ``X-LSW-Auth: <API-KEY>`` header (no ``Bearer`` prefix).
- Catalog reads: ``GET /ordering/v1/products/vps`` (list, per location) and
  ``GET /ordering/v1/products/vps/{vpsId}`` (detail with OS/config options
  and per-term prices).
- Ordering: ``POST /ordering/v1/products/vps/{vpsId}/order`` ->
  ``{orderId}`` (201). The order id is NEVER treated as the server id;
  reconciliation inspects ``GET /account/v1/orders/{Id}`` and resolves the
  provisioned VPS through ``GET /publicCloud/v1/vps``.
- Management: ``GET /publicCloud/v1/vps``, ``GET /publicCloud/v1/vps/{id}``
  (state, IPs, contract/renewal data), ``POST .../start|stop|reboot``.
- Idempotency: the Ordering API documents no ``Idempotency-Key`` header and
  the order body carries no reference field, so the adapter enforces
  get-before-create platform-side: before POSTing, recent orders are
  scanned for a matching NEW_ORDER (VIRTUAL_SERVER, same location/price,
  recent window) and a hit is returned as the result of the earlier attempt.
- Error mapping, throttling and retry honor the shared provider discipline
  (``_error_payload`` / ``_raise_for_status`` / ``Throttle`` from the
  Public Cloud adapter); the API key never appears in errors or logs.

No live order is ever placed from tests: tests mock the HTTP transport, and
the only verification path that may POST a real order (``leaseweb
smoke-order`` in the CLI) requires ``LEASEWEB_ALLOW_LIVE_ORDER_TEST=true``
(default false).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.observability.metrics import metrics
from cloud_platform.providers.base import (
    Capability,
    CreateServerRequest,
    OrderingProvider,
    ProviderImage,
    ProviderLocation,
    ProviderPlan,
    ProviderServer,
    ProvisioningTicket,
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
from cloud_platform.providers.leaseweb.client import (
    Throttle,
    _as_list,
    _error_payload,
    _operation_label,
    _parse_retry_after,
    normalize_provider_status,
)

#: Advertised capabilities: COMPUTE (order + manage) and POWER (start/stop/
#: reboot). Delete/rebuild/snapshot are NOT advertised: the current VPS API
#: has no verified cancel/delete endpoint (cancellation is manual), and
#: rebuild/snapshot are out of MVP scope.
LEASEWEB_ORDERING_CAPABILITIES = frozenset({Capability.COMPUTE, Capability.POWER})

#: Monthly contract/billing defaults for resold VPSes (MVP).
DEFAULT_CONTRACT_TERM = "1_MONTH"
DEFAULT_BILLING_CYCLE = "1_MONTH"

#: How far back a get-before-create scan looks for a matching order.
ORDER_MATCH_WINDOW = timedelta(minutes=30)

#: Order service statuses (orders API) -> coarse ticket state.
_ORDER_STATUS_MAP: dict[str, str] = {
    "NEW_CONTRACT": "provisioning",
    "SCHEDULED": "provisioning",
    "TO_BE_PROVISIONED": "provisioning",
    "ACTIVE": "provisioned",
    "CANCELLED": "failed",
    "SUSPENDED": "failed",
}

#: Datacenter code -> (country, city) display metadata. This is operator
#: display data for the customer UI (geography, not pricing); unknown codes
#: fall back to the code itself.
LOCATION_DISPLAY: dict[str, tuple[str, str]] = {
    "AMS-01": ("NL", "Amsterdam"),
    "FRA-01": ("DE", "Frankfurt"),
    "LON-01": ("GB", "London"),
    "WDC-02": ("US", "Washington"),
    "SFO-12": ("US", "San Francisco"),
    "LAX-12": ("US", "Los Angeles"),
    "MTL-02": ("CA", "Montreal"),
    "SIN-01": ("SG", "Singapore"),
    "TYO-11": ("JP", "Tokyo"),
    "SYD-12": ("AU", "Sydney"),
}


class VpsMatchAmbiguous(ProviderError):
    """More than one VPS plausibly matches an order; a human must decide."""


def _decimal(value: Any) -> Decimal | None:
    """Parse a provider price value into Decimal (never float arithmetic)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def to_minor_units(value: Decimal) -> int:
    """Decimal major units -> integer minor units, half-up."""
    return int((value * Decimal(100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _minor(value: Any) -> int:
    """Provider ``total``/``price`` value -> minor units (0 when unparsable)."""
    parsed = _decimal(value)
    return to_minor_units(parsed) if parsed is not None else 0


@dataclass(frozen=True, slots=True)
class LeasewebProductOption:
    """One configuration option (OS / control panel / SLA / disk)."""

    name: str
    price_minor: int
    currency: str
    selected: bool

    @property
    def is_free(self) -> bool:
        return self.price_minor == 0


@dataclass(frozen=True, slots=True)
class LeasewebProduct:
    """One sellable VPS product at one location (list endpoint payload)."""

    id: str
    name: str
    location: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    traffic: str
    currency: str
    monthly_price_minor: int
    provider_price_minor: int  # raw ``price.total`` (may include term discounts)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LeasewebProductDetail:
    """Full product detail: specs + configuration options + per-term prices."""

    product: LeasewebProduct
    os_options: tuple[LeasewebProductOption, ...]
    control_panels: tuple[LeasewebProductOption, ...]
    disk_upgrades: tuple[LeasewebProductOption, ...]
    slas: tuple[LeasewebProductOption, ...]
    available_locations: tuple[str, ...]
    contract_terms: dict[str, int]  # key -> monthly price (minor units)
    billing_cycles: dict[str, int]

    def free_os_options(self) -> tuple[LeasewebProductOption, ...]:
        """The OS options that do not change the base price (MVP selection)."""
        return tuple(option for option in self.os_options if option.is_free)


def _parse_product(item: dict[str, Any], location: str) -> LeasewebProduct | None:
    raw_id = str(item.get("id") or "").strip()
    if not raw_id:
        return None
    price = item.get("price")
    price_dict = price if isinstance(price, dict) else {}
    currency = str(price_dict.get("currency") or "EUR")
    return LeasewebProduct(
        id=raw_id,
        name=str(item.get("name") or raw_id),
        location=location,
        vcpu=_int_of(item.get("vCpu")),
        ram_gb=_int_of(item.get("vRam")),
        disk_gb=_disk_gb(item.get("nvmeStorage")),
        traffic=str(item.get("traffic") or ""),
        currency=currency,
        monthly_price_minor=_minor(price_dict.get("total")),
        provider_price_minor=_minor(price_dict.get("total")),
        metadata={"base_price_minor": _minor(price_dict.get("basePrice"))},
    )


def _int_of(value: Any) -> int:
    """Parse a possibly-string integer (``vCpu: '4'``)."""
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return 0


def _disk_gb(value: Any) -> int:
    """Parse ``'100 GB'`` -> 100."""
    if not value:
        return 0
    text = str(value).strip().upper()
    if text.endswith("GB"):
        return _int_of(text[:-2].strip())
    if text.endswith("TB"):
        return _int_of(text[:-2].strip()) * 1024
    return _int_of(text)


def _parse_option(item: dict[str, Any]) -> LeasewebProductOption:
    return LeasewebProductOption(
        name=str(item.get("name") or ""),
        price_minor=_minor(item.get("price")),
        currency=str(item.get("currency") or "EUR"),
        selected=bool(item.get("selected", False)),
    )


def _options(payload: Any) -> tuple[LeasewebProductOption, ...]:
    return tuple(_parse_option(i) for i in _as_list(payload, "options", "items"))


def _term_totals(price: dict[str, Any]) -> dict[str, int]:
    """``contractTerms``/``billingCycles`` -> {key: monthly minor units}."""
    out: dict[str, int] = {}
    for key in ("contractTerms", "billingCycles"):
        for row in _as_list(price, key):
            if not isinstance(row, dict):
                continue
            term_key = str(row.get("key") or "").strip()
            if term_key:
                out[f"{key[:-1]}:{term_key}"] = _minor(row.get("total"))
    return out


class LeaseWebOrderingProvider(OrderingProvider):
    """Leaseweb ordering-VPS adapter (provider key ``leaseweb``)."""

    key = "leaseweb"
    capabilities = LEASEWEB_ORDERING_CAPABILITIES

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.leaseweb.com",
        locations: tuple[str, ...] = ("AMS-01", "FRA-01"),
        contract_term: str = DEFAULT_CONTRACT_TERM,
        billing_cycle: str = DEFAULT_BILLING_CYCLE,
        os_allowlist: tuple[str, ...] = (),
        order_os_only_free: bool = True,
        throttle: Throttle | None = None,
        max_retries: int = 3,
        credential_source: CredentialSource | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if not locations:
            raise ValueError("locations must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if credential_source is not None:
            default_headers = {"Accept": "application/json", "Content-Type": "application/json"}
        else:
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
        self._locations = tuple(locations)
        self._contract_term = contract_term
        self._billing_cycle = billing_cycle
        self._os_allowlist = tuple(os_allowlist)
        self._order_os_only_free = order_os_only_free
        self._throttle = throttle or Throttle()
        self._max_retries = max_retries

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # CloudProvider read port (ordering catalog)
    # ------------------------------------------------------------------

    async def list_locations(self) -> list[ProviderLocation]:
        """The configured ordering locations (the ordering API has no
        location-list endpoint; the allowlist IS the sync scope)."""
        return [
            ProviderLocation(
                id=code,
                name=code,
                country_code=LOCATION_DISPLAY.get(code, ("NL", code))[0],
                city=LOCATION_DISPLAY.get(code, ("NL", ""))[1] or None,
                metadata={"source": "leaseweb-ordering-config"},
            )
            for code in self._locations
        ]

    async def list_plans(self) -> list[ProviderPlan]:
        """Union of ordering products across the configured locations."""
        plans: list[ProviderPlan] = []
        seen: set[tuple[str, str]] = set()
        for location in self._locations:
            for product in await self.list_products(location):
                if (product.id, location) in seen:
                    continue
                seen.add((product.id, location))
                plans.append(
                    ProviderPlan(
                        id=product.id,
                        name=product.name,
                        architecture="x86_64",
                        vcpu=product.vcpu,
                        memory_mb=product.ram_gb * 1024,
                        disk_gb=product.disk_gb,
                        metadata={
                            "traffic": product.traffic,
                            "monthly_price_minor": product.monthly_price_minor,
                            "currency": product.currency,
                            "location": location,
                        },
                    )
                )
        return plans

    async def list_images(self) -> list[ProviderImage]:
        """Union of free OS options across the configured locations."""
        images: list[ProviderImage] = []
        seen: set[str] = set()
        for location in self._locations:
            for product in await self.list_products(location):
                try:
                    detail = await self.get_product(location, product.id)
                except ProviderError:
                    continue
                for option in detail.free_os_options():
                    if option.name in seen:
                        continue
                    seen.add(option.name)
                    images.append(
                        ProviderImage(
                            id=option.name,
                            name=option.name,
                            os_family="linux",
                            architecture="x86_64",
                            metadata={"location": location, "product_id": product.id},
                        )
                    )
        return images

    # ------------------------------------------------------------------
    # Ordering catalog (location-scoped)
    # ------------------------------------------------------------------

    async def list_products(self, location: str) -> list[LeasewebProduct]:
        """``GET /ordering/v1/products/vps?location=`` (paginated)."""
        products: list[LeasewebProduct] = []
        offset = 0
        while True:
            payload = await self._request(
                "GET",
                "/ordering/v1/products/vps",
                params={"location": location, "limit": 100, "offset": offset},
            )
            items = _as_list(payload, "vpss", "products", "data", "items")
            products.extend(p for p in (_parse_product(i, location) for i in items) if p)
            meta = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
            total = meta.get("totalCount")
            if not isinstance(total, int) or len(products) >= total or not items:
                return products
            offset += len(items)

    async def get_product(
        self,
        location: str,
        product_id: str,
        *,
        contract_term: str | None = None,
        billing_cycle: str | None = None,
    ) -> LeasewebProductDetail:
        """``GET /ordering/v1/products/vps/{vpsId}?location=`` with our
        contract/billing terms, so every price reflects what we order."""
        params: dict[str, Any] = {"location": location}
        params["contractTerm"] = contract_term or self._contract_term
        params["billingCycle"] = billing_cycle or self._billing_cycle
        payload = await self._request(
            "GET", f"/ordering/v1/products/vps/{product_id}", params=params
        )
        item = payload.get("vps") if isinstance(payload, dict) else None
        if not isinstance(item, dict):
            item = payload if isinstance(payload, dict) else {}
        product = _parse_product(item, location)
        if product is None:
            raise ProviderError(f"leaseweb product {product_id!r} at {location!r} not parseable")

        price = item.get("price")
        price_dict = price if isinstance(price, dict) else {}
        terms = _term_totals(price_dict)
        term_key = f"contractTerm:{self._contract_term}"
        cycle_key = f"billingCycle:{self._billing_cycle}"
        monthly = terms.get(term_key) or terms.get(cycle_key) or _minor(price_dict.get("total"))
        product = LeasewebProduct(
            id=product.id,
            name=product.name,
            location=location,
            vcpu=product.vcpu,
            ram_gb=product.ram_gb,
            disk_gb=product.disk_gb,
            traffic=product.traffic,
            currency=product.currency,
            monthly_price_minor=monthly,
            provider_price_minor=_minor(price_dict.get("total")),
            metadata=product.metadata,
        )

        config = item.get("configurationOptions")
        config_dict = config if isinstance(config, dict) else {}
        os_options = _options(config_dict.get("operatingSystem"))
        if self._os_allowlist:
            allowed = {name.lower() for name in self._os_allowlist}
            os_options = tuple(o for o in os_options if o.name.lower() in allowed)
        locations_raw = item.get("location")
        locations: tuple[str, ...] = ()
        if isinstance(locations_raw, list):
            locations = tuple(str(x) for x in locations_raw)
        return LeasewebProductDetail(
            product=product,
            os_options=os_options,
            control_panels=_options(config_dict.get("controlPanel")),
            disk_upgrades=_options(config_dict.get("diskUpgrade")),
            slas=_options(config_dict.get("serviceLevelAgreement")),
            available_locations=locations,
            contract_terms={
                key.removeprefix("contractTerm:"): value
                for key, value in terms.items()
                if key.startswith("contractTerm:")
            },
            billing_cycles={
                key.removeprefix("billingCycle:"): value
                for key, value in terms.items()
                if key.startswith("billingCycle:")
            },
        )

    def os_name_allowed(self, detail: LeasewebProductDetail, os_name: str) -> bool:
        """Server-side OS validation: the OS must be an option of the product
        and (by default) must not change the base price."""
        for option in detail.os_options:
            if option.name != os_name:
                continue
            if self._order_os_only_free and not option.is_free:
                return False
            return True
        return False

    # ------------------------------------------------------------------
    # Ordering port (asynchronous provisioning)
    # ------------------------------------------------------------------

    async def place_order(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProvisioningTicket:
        """POST the order once per operation key.

        Get-before-create: a matching NEW_ORDER from a previous attempt is
        returned instead of POSTing again (the Ordering API has no
        Idempotency-Key header and no reference field to correlate on).
        """
        del idempotency_key  # platform ledger owns dedup; scan is a backstop
        existing = await self._find_recent_order(
            location=request.location_id,
            product_id=request.plan_id,
            price_minor=request_price_minor(request),
        )
        if existing is not None:
            return existing
        body: dict[str, str] = {
            "location": request.location_id,
            "operatingSystem": request.image_id,
            "contractTerm": self._contract_term,
            "billingCycle": self._billing_cycle,
        }
        payload = await self._request(
            "POST", f"/ordering/v1/products/vps/{request.plan_id}/order", json=body
        )
        if not isinstance(payload, dict):
            raise ProviderError("leaseweb order accepted with an unexpected payload")
        order_id = payload.get("orderId")
        if order_id is None:
            raise ProviderError("leaseweb order response is missing orderId")
        return ProvisioningTicket(
            provider_order_id=str(order_id),
            state="accepted",
            metadata={
                "location": request.location_id,
                "product_id": request.plan_id,
                "operating_system": request.image_id,
                "contract_term": self._contract_term,
                "billing_cycle": self._billing_cycle,
            },
        )

    async def _find_recent_order(
        self, *, location: str, product_id: str, price_minor: int
    ) -> ProvisioningTicket | None:
        """Best-effort provider-side dedup: scan recent NEW_ORDERs for a
        matching VIRTUAL_SERVER order (same location/price, recent window)."""
        try:
            payload = await self._request(
                "GET", "/account/v1/orders", params={"limit": 50, "offset": 0}
            )
        except ProviderError:
            return None
        cutoff = datetime.now(UTC) - ORDER_MATCH_WINDOW
        for row in _as_list(payload, "orders"):
            if not isinstance(row, dict) or str(row.get("type") or "") != "NEW_ORDER":
                continue
            created = _parse_datetime(row.get("createdAt"))
            if created is None or created < cutoff:
                continue
            for service in _as_list(row, "services"):
                if not isinstance(service, dict):
                    continue
                if str(service.get("productId") or "") != "VIRTUAL_SERVER":
                    continue
                if abs(_minor(service.get("pricePerFrequency")) - price_minor) > 1:
                    continue
                return ProvisioningTicket(
                    provider_order_id=str(row.get("id") or ""),
                    state="provisioning",
                    metadata={
                        "matched_existing": True,
                        "location": location,
                        "product_id": product_id,
                    },
                )
        return None

    async def get_order(self, provider_order_id: str) -> ProvisioningTicket:
        """``GET /account/v1/orders/{Id}`` -> coarse ticket state."""
        payload = await self._request("GET", f"/account/v1/orders/{provider_order_id}")
        if not isinstance(payload, dict):
            raise ProviderError(f"leaseweb order {provider_order_id} not parseable")
        service = _first_service(payload)
        status = str(service.get("status") or "").upper()
        state = _ORDER_STATUS_MAP.get(status, "provisioning")
        return ProvisioningTicket(
            provider_order_id=str(payload.get("id") or provider_order_id),
            state=state,
            provider_resource_id=_clean_id(service.get("equipmentId")),
            metadata={
                "contract_id": _clean_id(payload.get("contractId")),
                "service_id": _clean_id(service.get("id")),
                "order_status": status,
                "delivery_estimate": service.get("deliveryEstimate"),
                "price_per_frequency_minor": _minor(service.get("pricePerFrequency")),
                "currency": service.get("currency"),
                "contract_term": service.get("contractTerm"),
                "billing_cycle": service.get("billingCycle"),
            },
        )

    # ------------------------------------------------------------------
    # Provisioned VPS management (CloudProvider port)
    # ------------------------------------------------------------------

    async def get_server(self, provider_server_id: str) -> ProviderServer | None:
        """``GET /publicCloud/v1/vps/{vpsId}`` -> ProviderServer."""
        try:
            payload = await self._request("GET", f"/publicCloud/v1/vps/{provider_server_id}")
        except ProviderNotFound:
            return None
        if not isinstance(payload, dict):
            return None
        return self._map_vps(payload)

    async def list_servers(self) -> list[ProviderServer]:
        params: dict[str, Any] = {"limit": 100, "offset": 0}
        servers: list[ProviderServer] = []
        while True:
            payload = await self._request("GET", "/publicCloud/v1/vps", params=params)
            items = _as_list(payload, "vps", "data", "items")
            servers.extend(self._map_vps(i) for i in items if isinstance(i, dict))
            meta = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
            total = meta.get("totalCount")
            if not isinstance(total, int) or len(servers) >= total or not items:
                return servers
            params["offset"] = int(params["offset"]) + len(items)

    async def match_vps_for_order(
        self,
        provider_order_id: str,
        *,
        location: str,
        product_name: str,
        since: datetime,
    ) -> str:
        """Resolve the provisioned VPS id for an ACTIVE order.

        1. The order service's ``equipmentId``, when it identifies a VPS.
        2. Otherwise the VPS list is matched on (datacenter, pack, startedAt
           window); exactly one match wins, none is NotFound, several is
           :class:`VpsMatchAmbiguous` (a human must decide).
        """
        order = await self.get_order(provider_order_id)
        equipment_id = order.provider_resource_id
        if equipment_id:
            try:
                vps = await self.get_server(equipment_id)
            except ProviderError:
                vps = None
            if vps is not None:
                return vps.id

        params: dict[str, Any] = {"limit": 100, "offset": 0}
        candidates: list[str] = []
        while True:
            payload = await self._request("GET", "/publicCloud/v1/vps", params=params)
            items = _as_list(payload, "vps", "data", "items")
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("datacenter") or "") != location:
                    continue
                if str(item.get("pack") or "") != product_name:
                    continue
                started = _parse_datetime(item.get("startedAt"))
                if started is not None and (started < since - timedelta(hours=2)):
                    continue
                candidates.append(str(item.get("id") or ""))
            meta = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
            total = meta.get("totalCount")
            if not isinstance(total, int) or len(candidates) >= total or not items:
                break
            params["offset"] = int(params["offset"]) + len(items)
        unique = sorted(set(c for c in candidates if c))
        if len(unique) == 1:
            return unique[0]
        if len(unique) > 1:
            raise VpsMatchAmbiguous(
                f"order {provider_order_id}: {len(unique)} VPSes match "
                f"{location}/{product_name}; manual review required"
            )
        raise ProviderNotFound(
            f"order {provider_order_id}: no provisioned VPS found yet at "
            f"{location} for {product_name!r}"
        )

    async def get_vps_credentials(self, provider_server_id: str) -> list[dict[str, str]]:
        """``GET /publicCloud/v1/vps/{vpsId}/credentials`` (usernames only;
        passwords are never fetched or stored by the platform)."""
        payload = await self._request(
            "GET", f"/publicCloud/v1/vps/{provider_server_id}/credentials"
        )
        out: list[dict[str, str]] = []
        for row in _as_list(payload, "credentials"):
            if not isinstance(row, dict):
                continue
            username = row.get("username")
            if username:
                out.append({"type": str(row.get("type") or ""), "username": str(username)})
        return out

    async def create_server(
        self, request: CreateServerRequest, idempotency_key: IdempotencyKey
    ) -> ProviderServer:
        """Synchronous creates are not supported: monthly products go
        through :meth:`place_order` (the ordering port). Failing loudly here
        keeps the hourly path from silently misusing this adapter."""
        del request, idempotency_key
        raise ProviderError(
            "leaseweb ordering VPS is an asynchronous monthly product; use "
            "the ordering port (place_order) instead of create_server"
        )

    async def delete_server(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        """Not supported by the Leaseweb VPS API (no verified cancel endpoint)."""
        del provider_server_id, idempotency_key
        raise ProviderError(
            "leaseweb ordering VPS has no delete API; cancellation is a manual "
            "portal operation (see docs/operations/RUNBOOK.md)"
        )

    async def power_on(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/publicCloud/v1/vps/{provider_server_id}/start")

    async def power_off(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/publicCloud/v1/vps/{provider_server_id}/stop")

    async def reboot(self, provider_server_id: str, idempotency_key: IdempotencyKey) -> None:
        del idempotency_key
        await self._request("POST", f"/publicCloud/v1/vps/{provider_server_id}/reboot")

    async def verify_credential(self, candidate: str) -> None:
        """Read-only connectivity check with a CANDIDATE key."""
        response = await self._client.request(
            "GET",
            "/ordering/v1/products/vps",
            params={"location": self._locations[0], "limit": 1},
            headers={"X-LSW-Auth": candidate},
        )
        self._raise_for_status(response)

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

    def _map_vps(self, item: dict[str, Any]) -> ProviderServer:
        raw_id = str(item.get("id") or "")
        ipv4: str | None = None
        ipv6: str | None = None
        for entry in _as_list(item, "ips"):
            if not isinstance(entry, dict):
                continue
            value = str(entry.get("ip") or "")
            version = str(entry.get("version") or "")
            if "6" in version or ":" in value:
                if ipv6 is None:
                    ipv6 = value
            elif ipv4 is None:
                ipv4 = value
        contract = item.get("contract")
        contract_dict = contract if isinstance(contract, dict) else {}
        state = str(item.get("state") or "unknown")
        return ProviderServer(
            id=raw_id,
            name=str(item.get("reference") or item.get("pack") or raw_id),
            status=normalize_provider_status(state),
            ipv4=ipv4,
            ipv6=ipv6,
            metadata={
                "raw_state": state,
                "datacenter": item.get("datacenter"),
                "region": item.get("region"),
                "pack": item.get("pack"),
                "image": item.get("image"),
                "root_disk_gb": item.get("rootDiskSize"),
                "started_at": item.get("startedAt"),
                "contract_id": contract_dict.get("id"),
                "contract_state": contract_dict.get("state"),
                "contract_starts_at": contract_dict.get("startsAt"),
                "contract_ends_at": contract_dict.get("endsAt"),
                "contract_type": contract_dict.get("type"),
                "contract_term": contract_dict.get("term"),
                "billing_frequency": contract_dict.get("billingFrequency"),
                "sla": contract_dict.get("sla"),
                "control_panel": contract_dict.get("controlPanel"),
            },
        )


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _clean_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_service(payload: dict[str, Any]) -> dict[str, Any]:
    for row in _as_list(payload, "services"):
        if isinstance(row, dict):
            return row
    return {}


def request_price_minor(request: CreateServerRequest) -> int:
    """The price snapshot carried on the create request, when present.

    The domain passes the exact monthly selling price via ``labels``
    (``price_minor``), which the get-before-create scan compares against the
    order's ``pricePerFrequency`` (major-unit EUR; tolerance 1 cent).
    """
    raw = request.labels.get("price_minor", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


# Back-compat alias (same convention as the Public Cloud adapter).
LeasewebOrderingProvider = LeaseWebOrderingProvider
