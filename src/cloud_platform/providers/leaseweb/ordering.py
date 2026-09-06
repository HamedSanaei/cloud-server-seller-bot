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
- Idempotency limitation (verified against the official specs at
  github.com/leaseweb/api-definitions): the Ordering API documents no
  ``Idempotency-Key`` header and the order body carries no client reference
  or correlation field, so **mathematically exactly-once provider ordering
  is not possible**. The platform therefore guarantees exactly-once LOCAL
  wallet effects and operation identity, at-most-one automatic POST per
  operation, and reconciliation/manual review before any second chargeable
  POST is ever considered.
- Ledger-owned dedup (release hardening): a fresh claimed operation ALWAYS
  POSTs exactly once. Account-wide order similarity is NEVER used to
  suppress or reuse an order — two independent customers may legitimately
  buy the same plan at the same price in the same location, and the Orders
  API cannot tell their orders apart (it exposes no exact VPS product id,
  location, OS or client reference). The platform operation ledger
  (atomic claim + deterministic key) owns dedup.
- Read-only recovery: an ambiguous POST (``PROVIDER_OUTCOME_UNKNOWN``) is
  resolved by a READ-ONLY scan that counts recent NEW_ORDER
  VIRTUAL_SERVER candidates by PROVIDER-side facts only (provider price
  snapshot, currency, contract term, billing cycle, creation window —
  NEVER the customer selling price). Because generic similarity cannot
  PROVE ownership, ANY candidate count (0, 1 or many) escalates to manual
  review: this adapter never auto-attaches a candidate (it never returns
  a MATCHED verdict).
- Ambiguous-outcome classification: a billable POST whose result cannot be
  proven (read/write timeout, dropped connection, 5xx after transmission)
  raises :class:`ProviderOutcomeUnknown` — the platform records the
  operation as outcome-unknown and never automatically re-POSTs it.
  Only errors that PROVE the request was never transmitted (connect
  refused/timeout, pool timeout) stay retryable.
- Error mapping, throttling and retry honor the shared provider discipline
  (``_error_payload`` / ``_raise_for_status`` / ``Throttle`` from the
  Public Cloud adapter); the API key never appears in errors or logs.

No live order is ever placed from tests: tests mock the HTTP transport, and
NO CLI command can POST a real order — every billable POST must flow
through the durable checkout -> order worker pipeline, so an ambiguous
outcome is always persisted and recoverable.
"""

from __future__ import annotations

import logging
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
    OrderRecoveryResult,
    OrderRecoveryVerdict,
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
    ProviderOutcomeUnknown,
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

logger = logging.getLogger(__name__)

#: Advertised capabilities: COMPUTE (order + manage) and POWER (start/stop/
#: reboot). Delete/rebuild/snapshot are NOT advertised: the current VPS API
#: has no verified cancel/delete endpoint (cancellation is manual), and
#: rebuild/snapshot are out of MVP scope.
LEASEWEB_ORDERING_CAPABILITIES = frozenset({Capability.COMPUTE, Capability.POWER})

#: Monthly contract/billing defaults for resold VPSes (MVP).
DEFAULT_CONTRACT_TERM = "1_MONTH"
DEFAULT_BILLING_CYCLE = "1_MONTH"

#: How far back the READ-ONLY recovery scan looks for candidate orders
#: matching the provider facts of an OUTCOME_UNKNOWN operation. This scan
#: only COUNTS candidates — it never auto-attaches one — and the result is
#: escalated to a human for review.
ORDER_MATCH_WINDOW = timedelta(minutes=30)

#: Per-page size for order scans (bounded by the provider's metadata total).
ORDER_SCAN_PAGE = 100

#: Price tolerance (minor units) when comparing provider price snapshots to
#: the order's ``pricePerFrequency`` (the API returns major-unit floats).
PRICE_TOLERANCE_MINOR = 1

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
        """POST the order exactly once per claimed local operation.

        The Ordering API has no Idempotency-Key header and no client
        reference field (verified against the official spec), so
        provider-side exactly-once ordering cannot be guaranteed. The
        platform operation ledger (atomic claim + deterministic key
        ``order-create:{server_id}``) is the ONLY dedup mechanism: this
        method always POSTs for a claimed operation and NEVER scans recent
        account orders to guess whether an earlier attempt exists.

        Account-wide similarity (VIRTUAL_SERVER + price + currency + term +
        cycle + time) is deliberately NOT used to suppress or reuse an
        order: two independent customers can legitimately buy the exact
        same plan at the same price in the same location, and the Orders
        API cannot tell their orders apart (no exact product id, location,
        OS or client reference).

        Outcomes:
        - 201 with an ``orderId``      -> ``accepted`` ticket.
        - definitive provider rejection -> the mapped provider error; no
          order was created and the caller releases the hold.
        - ambiguous (transport error after transmission, 5xx, mutating
          429, unreadable/missing ``orderId``) ->
          :class:`ProviderOutcomeUnknown`: the caller must NEVER blindly
          re-POST; the operation becomes OUTCOME_UNKNOWN and is resolved
          by READ-ONLY recovery or human review.
        """
        del idempotency_key  # the platform ledger owns local dedup
        provider_cost = request_provider_price_minor(request)
        body: dict[str, str] = {
            "location": request.location_id,
            "operatingSystem": request.image_id,
            "contractTerm": request.labels.get("contract_term") or self._contract_term,
            "billingCycle": request.labels.get("billing_cycle") or self._billing_cycle,
        }
        payload = await self._request(
            "POST",
            f"/ordering/v1/products/vps/{request.plan_id}/order",
            json=body,
            mutating=True,
        )
        if not isinstance(payload, dict):
            raise ProviderOutcomeUnknown(
                "leaseweb order POST returned an unexpected payload; outcome unknown"
            )
        order_id = payload.get("orderId")
        if order_id is None:
            raise ProviderOutcomeUnknown(
                "leaseweb order POST response is missing orderId; outcome unknown"
            )
        return ProvisioningTicket(
            provider_order_id=str(order_id),
            state="accepted",
            metadata={
                "location": request.location_id,
                "product_id": request.plan_id,
                "operating_system": request.image_id,
                "contract_term": body["contractTerm"],
                "billing_cycle": body["billingCycle"],
                "provider_cost_minor": str(provider_cost),
                "provider_currency": request.labels.get("provider_currency") or "",
            },
        )

    async def recover_order(
        self,
        *,
        provider_cost_minor: int,
        currency: str,
        contract_term: str,
        billing_cycle: str,
        since: datetime,
    ) -> OrderRecoveryResult:
        """READ-ONLY recovery for an OUTCOME_UNKNOWN operation (release
        hardening).

        The Orders API exposes NO provider-side identifier that correlates
        an order to the exact ordering VPS product, location,
        OS/configuration or to a client reference (verified against the
        official spec). Generic similarity — VIRTUAL_SERVER family,
        provider price snapshot, currency, term, cycle, creation window —
        can therefore NEVER PROVE that a candidate belongs to the local
        operation: another customer's identical order satisfies the same
        facts. Attaching the wrong order would capture the wrong wallet and
        could double-provision, so the platform prefers manual review.

        Verdicts (this adapter NEVER returns MATCHED):
        - zero candidates   -> NO_MATCH: absence is NOT provable (the order
          may be invisible yet or outside the window); escalate.
        - one or more       -> AMBIGUOUS: unproven candidate(s); a human
          decides. The candidate ids are reported in the reason.
        - scan failed       -> SCAN_FAILED (transient; bounded retries).
        """
        try:
            matches = await self._scan_matching_orders(
                provider_cost_minor=provider_cost_minor,
                currency=currency,
                contract_term=contract_term,
                billing_cycle=billing_cycle,
                since=since,
            )
        except ProviderError as exc:
            return OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.SCAN_FAILED,
                candidate_count=0,
                reason=f"recovery scan failed ({type(exc).__name__}): {exc}",
            )
        if not matches:
            return OrderRecoveryResult(
                verdict=OrderRecoveryVerdict.NO_MATCH,
                candidate_count=0,
                reason="no matching NEW_ORDER found in the scan window; absence cannot be proven",
            )
        # One OR several candidates: the Orders API cannot prove that any of
        # them belongs to this operation (no exact product/location/OS or
        # client reference), so even a single candidate is never attached.
        return OrderRecoveryResult(
            verdict=OrderRecoveryVerdict.AMBIGUOUS,
            candidate_count=len(matches),
            reason=(
                f"{len(matches)} generic candidate(s) match the provider facts "
                f"({', '.join(matches)}); the Orders API cannot prove which "
                "belongs to this operation — manual review required"
            ),
        )

    async def _scan_matching_orders(
        self,
        *,
        provider_cost_minor: int,
        currency: str,
        contract_term: str,
        billing_cycle: str,
        since: datetime,
    ) -> list[str]:
        """Read-only: order ids of NEW_ORDER VIRTUAL_SERVER orders matching
        ALL the given provider-side facts.

        The orders API exposes only the product family (``VIRTUAL_SERVER``),
        ``pricePerFrequency``, ``currency``, ``contractTerm``,
        ``billingCycle`` and ``createdAt`` for a service — NOT the specific
        VPS product id, location or OS, and no client reference. Those
        fields are therefore never invented or compared here; the caller
        treats ANY candidate count as unproven and escalates to a human.
        """
        expected_term = _normalize_term(contract_term)
        expected_cycle = _normalize_term(billing_cycle)
        matches: list[str] = []
        offset = 0
        while True:
            payload = await self._request(
                "GET",
                "/account/v1/orders",
                params={"limit": ORDER_SCAN_PAGE, "offset": offset},
            )
            rows = _as_list(payload, "orders")
            for row in rows:
                if not isinstance(row, dict) or str(row.get("type") or "") != "NEW_ORDER":
                    continue
                created = _parse_datetime(row.get("createdAt"))
                if created is None or created < since or created > datetime.now(UTC):
                    continue
                if self._order_matches_facts(
                    row,
                    provider_cost_minor=provider_cost_minor,
                    currency=currency,
                    expected_term=expected_term,
                    expected_cycle=expected_cycle,
                ):
                    order_id = str(row.get("id") or "").strip()
                    if order_id:
                        matches.append(order_id)
            meta = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
            total = meta.get("totalCount")
            if (
                not isinstance(total, int)
                or len(rows) < ORDER_SCAN_PAGE
                or offset + len(rows) >= total
            ):
                break
            offset += len(rows)
        # One order may carry several matching services; dedupe order ids.
        return list(dict.fromkeys(matches))

    @staticmethod
    def _order_matches_facts(
        row: dict[str, Any],
        *,
        provider_cost_minor: int,
        currency: str,
        expected_term: str,
        expected_cycle: str,
    ) -> bool:
        """Whether ANY service of the order matches every provider fact."""
        for service in _as_list(row, "services"):
            if not isinstance(service, dict):
                continue
            if str(service.get("productId") or "") != "VIRTUAL_SERVER":
                continue
            if (
                provider_cost_minor > 0
                and abs(_minor(service.get("pricePerFrequency")) - provider_cost_minor)
                > PRICE_TOLERANCE_MINOR
            ):
                continue
            if currency and str(service.get("currency") or "").upper() != currency.upper():
                continue
            if expected_term and _normalize_term(service.get("contractTerm")) != expected_term:
                continue
            if expected_cycle and _normalize_term(service.get("billingCycle")) != expected_cycle:
                continue
            return True
        return False

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
                "product_id": str(service.get("productId") or ""),
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

        ONLY the provider-supported identity may auto-attach a VPS: the
        order service's ``equipmentId``, confirmed by a successful GET of
        that EXACT VPS id. There is NO fallback heuristic: datacenter + pack
        + startedAt similarity against the account VPS list is diagnostic
        evidence only and NEVER produces a provider_server_id, because the
        VPS API does not link a VPS to the ordering request that created it
        — a same-pack same-location VPS may belong to another customer's
        independent order.

        Raises :class:`ProviderNotFound` while the resource is not yet
        provably discoverable (no ``equipmentId`` yet, or the exact VPS GET
        fails): the caller keeps polling. A bounded wait policy (not a
        similarity heuristic) escalates to manual review.
        """
        order = await self.get_order(provider_order_id)
        equipment_id = order.provider_resource_id
        if equipment_id:
            # equipmentId is strong identity IF the exact VPS resolves.
            try:
                vps = await self.get_server(equipment_id)
            except ProviderError as exc:
                # 5xx/timeout on a READ-ONLY GET: keep waiting; never guess.
                raise ProviderNotFound(
                    f"order {provider_order_id}: equipmentId {equipment_id} "
                    f"not resolvable yet ({type(exc).__name__})"
                ) from exc
            if vps is not None:
                return vps.id
            raise ProviderNotFound(
                f"order {provider_order_id}: equipmentId {equipment_id} VPS not "
                "found yet (not provisioned or id not visible)"
            )

        # No equipmentId yet: STILL_PROVISIONING. The account VPS list is
        # scanned READ-ONLY for OPERATOR DIAGNOSTICS only — a same
        # (datacenter, pack) VPS is NOT proof of ownership, so no candidate
        # is ever returned or attached.
        await self._log_diagnostic_candidates(provider_order_id, location, product_name, since)
        raise ProviderNotFound(
            f"order {provider_order_id}: provider reports no equipmentId yet; "
            f"waiting for the exact resource identity (no heuristic attach)"
        )

    async def _log_diagnostic_candidates(
        self,
        provider_order_id: str,
        location: str,
        product_name: str,
        since: datetime,
    ) -> None:
        """Read-only VPS-list scan; logs similarity candidates for the
        operator but NEVER returns or attaches one (release hardening:
        account-wide similarity is not ownership)."""
        try:
            params: dict[str, Any] = {"limit": 100, "offset": 0}
            similar: list[str] = []
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
                    similar.append(str(item.get("id") or ""))
                meta = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
                total = meta.get("totalCount")
                if not isinstance(total, int) or len(similar) >= total or not items:
                    break
                params["offset"] = int(params["offset"]) + len(items)
            logger.info(
                "order %s: %d similar VPS(es) at %s/%s (diagnostics only, NOT attached)",
                provider_order_id,
                len({c for c in similar if c}),
                location,
                product_name,
            )
        except ProviderError as exc:
            logger.warning(
                "order %s: diagnostic VPS-list scan failed (%s); continuing to wait",
                provider_order_id,
                type(exc).__name__,
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

    async def _request(
        self, method: str, path: str, *, mutating: bool = False, **kwargs: Any
    ) -> Any:
        operation = _operation_label(method, path)
        async with metrics.provider_call(self.key, operation):
            return await self._perform_request(method, path, mutating=mutating, **kwargs)

    async def _perform_request(
        self, method: str, path: str, *, mutating: bool = False, **kwargs: Any
    ) -> Any:
        if self._credential_source is not None and "headers" not in kwargs:
            credential = await self._credential_source.get()
            kwargs["headers"] = {"X-LSW-Auth": credential.value}
        attempt = 0
        while True:
            await self._throttle.acquire()
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                # TransportError covers TimeoutException, NetworkError AND
                # RemoteProtocolError (connection dropped mid-stream).
                if mutating and _ambiguous_transport_error(exc):
                    # The request may have reached Leaseweb: the outcome of
                    # a billable POST is UNKNOWN. Never re-send blindly.
                    raise ProviderOutcomeUnknown(
                        f"leaseweb POST outcome unknown after transport error: {exc}"
                    ) from exc
                raise ProviderUnavailable(str(exc)) from exc
            if response.status_code != 429 or attempt >= self._max_retries or mutating:
                break
            # Read-only requests may honor the rate-limit pause in-adapter;
            # a billable POST is never re-sent inside the adapter: a mutating
            # 429 is classified conservatively as an unknown outcome (the
            # Leaseweb contract does not state that a 429 guarantees the
            # request was NOT processed).
            retry_after = response.headers.get("Retry-After")
            delay = _parse_retry_after(retry_after)
            if delay is None:
                delay = 0.5 * (2**attempt)
            await self._throttle.wait(min(delay, 30.0))
            attempt += 1
        return self._raise_for_status(response, mutating=mutating)

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, mutating: bool = False) -> Any:
        message = _error_payload(response)
        if response.status_code in (401, 403):
            raise ProviderAuthError(message)
        if response.status_code == 404:
            raise ProviderNotFound(message)
        if response.status_code in (409, 423) or "already" in message.lower():
            raise ProviderConflict(message)
        if response.status_code == 429:
            if mutating:
                # Conservative classification (release hardening): the
                # Leaseweb contract does NOT state that a 429 guarantees the
                # request was not processed — the order may exist. The
                # outcome of a billable POST is UNKNOWN and is never
                # automatically re-sent.
                raise ProviderOutcomeUnknown(
                    f"leaseweb order POST returned HTTP 429: outcome unknown; {message}"
                )
            retry_after = response.headers.get("Retry-After")
            raise ProviderRateLimited(message, _parse_retry_after(retry_after))
        if response.status_code >= 500:
            if mutating:
                # The order may have been accepted server-side even though
                # the response failed; the outcome is UNKNOWN, not "retry".
                raise ProviderOutcomeUnknown(
                    f"leaseweb order POST returned HTTP {response.status_code}: "
                    f"outcome unknown; {message}"
                )
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


def _normalize_term(value: Any) -> str:
    """Normalize a contract term / billing cycle for comparison.

    The Ordering API query parameters use underscore separators
    (``1_MONTH``) while the orders API reports space-separated values
    (``1 MONTH``); both normalize to the same key (``1MONTH``).
    """
    return str(value or "").strip().upper().replace("_", "").replace(" ", "").replace("-", "")


def _ambiguous_transport_error(exc: Exception) -> bool:
    """Whether a transport error proves the request was NEVER transmitted.

    ``ConnectError``/``ConnectTimeout``/``PoolTimeout`` fail before any
    bytes reach the server (no connection was established) — the mutation
    was definitely not applied, so a retry is safe. Every other transport
    failure (``ReadTimeout``, ``WriteTimeout``, ``RemoteProtocolError``,
    generic timeouts) may have occurred AFTER the request was transmitted:
    a billable POST's outcome is then unknown and must NOT be re-sent.
    """
    return not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))


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


def request_provider_price_minor(request: CreateServerRequest) -> int:
    """The PROVIDER cost snapshot carried on the create request.

    The domain passes the exact Leaseweb provider price (NOT the customer
    selling price) via the ``provider_price_minor`` label; the recovery
    scan compares it against the order's ``pricePerFrequency`` (major-unit
    currency; 1-cent tolerance). Selling price is NEVER used to identify a
    provider order — a reseller margin must not break recovery.
    """
    raw = request.labels.get("provider_price_minor", "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


# Back-compat alias (same convention as the Public Cloud adapter).
LeasewebOrderingProvider = LeaseWebOrderingProvider
