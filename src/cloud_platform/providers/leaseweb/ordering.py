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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.offers.domain import TechnicalSpec
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
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.client import (
    _as_list,
    normalize_provider_status,
)
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAmbiguousMutationError,
    LeasewebAuthenticationError,
    LeasewebError,
    LeasewebForbiddenError,
    LeasewebNotFoundError,
    LeasewebUnavailableError,
    LeasewebValidationError,
    error_for_response,
    parse_error_payload,
)
from cloud_platform.providers.leaseweb.models import to_minor_units
from cloud_platform.providers.leaseweb.ordering_api import LeaseWebOrderingApi
from cloud_platform.providers.leaseweb.orders_api import LeaseWebAccountOrdersApi
from cloud_platform.providers.leaseweb.transport import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    LeasewebTransport,
    MutationOutcome,
    Throttle,
    classify_transport_error,
)
from cloud_platform.providers.leaseweb.vps.client import LeaseWebVpsApi
from cloud_platform.providers.leaseweb.vps.management import LeaseWebVpsManagementMixin

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BILLING_CYCLE",
    "DEFAULT_CONTRACT_TERM",
    "KNOWN_VPS_DATACENTERS",
    "LEASEWEB_ORDERING_CAPABILITIES",
    "LOCATION_DISPLAY",
    "LeaseWebOrderingProvider",
    "LeasewebOrderingProvider",
    "LeasewebProduct",
    "LeasewebProductDetail",
    "LeasewebProductOption",
    "LocationEligibility",
    "LocationProbe",
    "VpsMatchAmbiguous",
    "classify_location_error",
    "extract_location_codes",
    "merge_candidates",
    "to_minor_units",
]

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

#: Upper bound on credential-verification probes: bounded work even when an
#: account is configured with dozens of seeds.
_MAX_VERIFY_LOCATIONS = 6

#: Well-known Leaseweb VPS datacenter codes. DISCOVERY SEEDS ONLY: every
#: code listed here is worth probing, but the probe result alone decides
#: whether the account may sell there. An ineligible seed simply yields
#: ``INELIGIBLE_ACCOUNT`` and stays out of sale; a location Leaseweb
#: enables tomorrow is picked up as soon as its probe succeeds — no
#: configuration change required. These are public geography facts, never
#: account scope and never prices.
KNOWN_VPS_DATACENTERS: tuple[str, ...] = (
    "AMS-01",
    "FRA-01",
    "FRA-10",
    "FRA-14",
    "LAX-12",
    "LON-01",
    "MTL-02",
    "SFO-12",
    "SIN-01",
    "SYD-12",
    "TYO-11",
    "WDC-02",
)


class LocationEligibility(StrEnum):
    """The verdict of one read-only location eligibility probe.

    Only the ``ELIGIBLE_*`` and ``INELIGIBLE_ACCOUNT`` verdicts are
    definitive (they prove current availability or its absence). The
    ``TRANSIENT_*`` verdicts preserve last-known provider availability;
    ``FATAL_AUTHENTICATION`` aborts the whole sync run.
    """

    #: 200 with one or more valid products: sellable (subject to the
    #: operator enabled/priced gate downstream).
    ELIGIBLE_AVAILABLE = "eligible_available"
    #: 200 with zero products: eligible but no current stock; hidden until
    #: products exist.
    ELIGIBLE_EMPTY = "eligible_empty"
    #: 403 on the ordering catalog: this account may not order here.
    INELIGIBLE_ACCOUNT = "ineligible_account"
    #: 429: throttled; retry the probe on the next refresh.
    TRANSIENT_THROTTLED = "transient_throttled"
    #: 5xx, timeouts and transport failures: unknown, preserve last-known.
    TRANSIENT_UNKNOWN = "transient_unknown"
    #: 401: the key itself is rejected; the whole run must stop.
    FATAL_AUTHENTICATION = "fatal_authentication"


@dataclass(frozen=True, slots=True)
class LocationProbe:
    """The outcome of probing one candidate location (read-only)."""

    location: str
    eligibility: LocationEligibility
    #: Products seen on a successful probe (empty otherwise).
    products: tuple[LeasewebProduct, ...] = ()
    #: Extra location codes worth probing, harvested from provider
    #: responses (detail ``location`` arrays, 403 messages).
    discovered_locations: tuple[str, ...] = ()
    #: Short human-safe summary (no secrets; safe to log and show).
    note: str = ""


#: Location codes look like ``FRA-01``: uppercase alpha, dash, digits.
_LOCATION_CODE_RE = re.compile(r"\b([A-Z]{2,5}-\d{1,3})\b")


def extract_location_codes(text: str) -> tuple[str, ...]:
    """Harvest datacenter codes from provider text (403 messages, payloads).

    Used only to grow the discovery candidate set; a harvested code still
    has to pass its own eligibility probe before anything is sold there.
    """
    if not text:
        return ()
    return tuple(sorted(set(_LOCATION_CODE_RE.findall(text))))


def _is_sales_organization_denial(message: str) -> bool:
    """Secondary discriminator for account-scope 403s.

    Matches both English spellings (``organization``/``organisation``).
    Isolated here (and unit-tested) so the wording can evolve without
    touching the classifier.
    """
    return "sales organi" in (message or "").lower()


def classify_location_error(exc: BaseException) -> tuple[LocationEligibility, str]:
    """Map a probe failure onto an eligibility verdict plus a safe note.

    The primary contract is the exception taxonomy (HTTP status mapped by
    the transport); the English message is only a secondary discriminator
    inside the 403 branch. The note never carries secrets: transport errors
    are redacted at construction.
    """
    if isinstance(exc, LeasewebAuthenticationError):
        return LocationEligibility.FATAL_AUTHENTICATION, "authentication rejected (401)"
    if isinstance(exc, LeasewebForbiddenError):
        if _is_sales_organization_denial(str(exc)):
            return LocationEligibility.INELIGIBLE_ACCOUNT, "not enabled for this sales organization"
        return LocationEligibility.INELIGIBLE_ACCOUNT, "forbidden for this account"
    if isinstance(exc, ProviderRateLimited):
        return LocationEligibility.TRANSIENT_THROTTLED, "throttled (429)"
    if isinstance(exc, ProviderUnavailable):
        return LocationEligibility.TRANSIENT_UNKNOWN, f"transient ({type(exc).__name__})"
    return LocationEligibility.TRANSIENT_UNKNOWN, f"transient ({type(exc).__name__})"


def merge_candidates(*sources: Iterable[str]) -> tuple[str, ...]:
    """Ordered, de-duplicated union of candidate location codes.

    Normalizes to stripped upper-case and drops empties. Order is
    deterministic: earlier sources win.
    """
    seen: set[str] = set()
    merged: list[str] = []
    for source in sources:
        for raw in source:
            code = (raw or "").strip().upper()
            if code and code not in seen:
                seen.add(code)
                merged.append(code)
    return tuple(merged)


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

#: City prefix (the ``XXX`` in ``XXX-NN``) -> (country, city). Adapter-internal
#: fallback ONLY: the ordering API exposes no location-list endpoint, so an
#: ordering-discovered code with no exact ``LOCATION_DISPLAY`` entry (a newer
#: hall in a known city, e.g. ``FRA-10``) would otherwise stay metadata-less.
#: The prefix convention is the provider's own code scheme, and the exact
#: table above always wins when present.
LOCATION_PREFIX_DISPLAY: dict[str, tuple[str, str]] = {
    "AMS": ("NL", "Amsterdam"),
    "FRA": ("DE", "Frankfurt"),
    "LAX": ("US", "Los Angeles"),
    "LON": ("GB", "London"),
    "MTL": ("CA", "Montreal"),
    "SFO": ("US", "San Francisco"),
    "SIN": ("SG", "Singapore"),
    "SYD": ("AU", "Sydney"),
    "TYO": ("JP", "Tokyo"),
    "WDC": ("US", "Washington"),
}


def describe_location_code(code: str) -> tuple[str, str, str]:
    """(country, city, source) for a location code, adapter-internal.

    Exact ``LOCATION_DISPLAY`` first, then the city-prefix fallback, then the
    verbatim code with no country. Never provider geography invented outside
    these adapter tables.
    """
    normalized = (code or "").strip().upper()
    if normalized in LOCATION_DISPLAY:
        country, city = LOCATION_DISPLAY[normalized]
        return country, city, "leaseweb-ordering-discovery"
    prefix, _, _ = normalized.partition("-")
    if prefix and prefix in LOCATION_PREFIX_DISPLAY:
        country, city = LOCATION_PREFIX_DISPLAY[prefix]
        return country, city, "leaseweb-ordering-prefix"
    return "", normalized, "leaseweb-ordering-unknown"


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
    #: Currency reported by the provider, or an EMPTY string when the response
    #: did not carry one. Sales Organizations bill in different currencies
    #: (EUR, GBP, ...), so a missing value is NEVER inferred to EUR: the write
    #: path fails closed instead of corrupting a known-correct price.
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
    # PROVIDER EVIDENCE ONLY: the product-level price currency, else the row's
    # own currency, else "not reported" (never a hard-coded EUR fallback).
    currency = str(price_dict.get("currency") or item.get("currency") or "").strip().upper()
    # The list payload names the storage key ``nvmeStorage``: its presence
    # proves NVMe storage for the normalized technical spec (the parsed
    # ``disk_gb`` alone cannot prove the storage technology).
    storage_marker: dict[str, object] = (
        {"storage_type": "NVMe"} if item.get("nvmeStorage") is not None else {}
    )
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
        metadata={"base_price_minor": _minor(price_dict.get("basePrice")), **storage_marker},
    )


def normalize_technical_spec(product: LeasewebProduct) -> TechnicalSpec:
    """Provider facts about one ordering product as a normalized spec.

    Only what the list/detail payloads actually state: the ordering VPS
    family is x86_64 (same claim ``list_plans`` already makes), NVMe storage
    when the payload carried the ``nvmeStorage`` key, and nothing else.
    IPv4/IPv6, CPU model and backup stay unknown — never guessed.
    """
    metadata = getattr(product, "metadata", None) or {}
    marker = metadata.get("storage_type") if isinstance(metadata, dict) else None
    return TechnicalSpec(
        architecture="x86_64",
        storage_type=str(marker) if marker else None,
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
        # Never inferred: an option whose currency the provider omitted stays
        # "not reported" rather than silently becoming EUR.
        currency=str(item.get("currency") or "").strip().upper(),
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


class LeaseWebOrderingProvider(LeaseWebVpsManagementMixin, OrderingProvider):
    """Leaseweb ordering-VPS adapter (provider key ``leaseweb``).

    Implements the provider-neutral ``OrderingProvider`` port plus the
    provider-neutral VPS management ports from
    :mod:`cloud_platform.providers.vps_ports` (inventory, power, console, ISO,
    reinstall, IPs, snapshots, metrics, monitoring, credentials,
    notifications) by delegating to the typed modern-VPS client. Application
    code therefore never constructs a Leaseweb URL.
    """

    key = "leaseweb"
    capabilities = LEASEWEB_ORDERING_CAPABILITIES

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        locations: tuple[str, ...] = (),
        contract_term: str = DEFAULT_CONTRACT_TERM,
        billing_cycle: str = DEFAULT_BILLING_CYCLE,
        os_allowlist: tuple[str, ...] = (),
        order_os_only_free: bool = True,
        throttle: Throttle | None = None,
        max_retries: int = 3,
        credential_source: CredentialSource | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        # ONE shared transport for the whole Leaseweb integration (ordering,
        # account orders and the VPS API all speak through it).
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
        self._locations = tuple(locations)
        self._contract_term = contract_term
        self._billing_cycle = billing_cycle
        self._os_allowlist = tuple(os_allowlist)
        self._order_os_only_free = order_os_only_free
        self._throttle = self._transport.throttle
        self._max_retries = max_retries
        #: Typed API surfaces built on the same transport (LEASEWEB-VPS-API):
        #: application/CLI code uses these instead of building URLs.
        self._ordering_api = LeaseWebOrderingApi(self._transport)
        self._orders_api = LeaseWebAccountOrdersApi(self._transport)
        self._vps_api = LeaseWebVpsApi(self._transport)

    @property
    def _client(self) -> Any:
        """The ONE transport's HTTP client (patched by tests)."""
        return self._transport.client

    @_client.setter
    def _client(self, client: Any) -> None:
        self._transport.set_client(client)

    # ------------------------------------------------------------------
    # Typed API surfaces (URLs never leave this provider package)
    # ------------------------------------------------------------------

    @property
    def ordering_api(self) -> LeaseWebOrderingApi:
        """Typed ordering-catalog client (list/detail/billable order)."""
        return self._ordering_api

    @property
    def orders_api(self) -> LeaseWebAccountOrdersApi:
        """Typed READ-ONLY account-orders client."""
        return self._orders_api

    @property
    def vps_api(self) -> LeaseWebVpsApi:
        """Typed modern-VPS API client (38 documented operations)."""
        return self._vps_api

    async def close(self) -> None:
        await self._transport.aclose()

    # ------------------------------------------------------------------
    # CloudProvider read port (ordering catalog)
    # ------------------------------------------------------------------

    @property
    def discovery_seeds(self) -> tuple[str, ...]:
        """Configured location codes as DISCOVERY SEEDS (never an allowlist).

        Seeds are only "locations worth probing": every seed still has to
        pass its own live eligibility probe before anything is sold there,
        and locations discovered elsewhere (provider payloads, persisted
        state) are probed too. An empty tuple is valid — discovery then
        relies on built-in seeds, persisted state and provider responses.
        """
        return self._locations

    def describe_location(self, code: str) -> ProviderLocation:
        """Display metadata for a location code without authorizing it.

        Unknown codes are retained verbatim (empty country, no city) so a
        newly enabled datacenter keeps working the moment it appears in a
        provider response.
        """
        normalized = (code or "").strip().upper()
        country, city, source = describe_location_code(normalized)
        return ProviderLocation(
            id=normalized,
            name=normalized,
            country_code=country,
            city=city or None,
            metadata={"source": source},
        )

    async def list_locations(self) -> list[ProviderLocation]:
        """The configured discovery seeds (NOT the sellability authority).

        The ordering API exposes no location-list endpoint; sellability is
        decided per location by the live eligibility probe
        (:meth:`probe_location`), never by this list.
        """
        return [self.describe_location(code) for code in self._locations]

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

    async def _list_products(
        self, params: dict[str, Any], default_location: str
    ) -> list[LeasewebProduct]:
        """One paginated ``GET /ordering/v1/products/vps`` read.

        Items that carry their own ``location`` string keep it (unscoped
        reads); otherwise the caller-supplied location is stamped.
        """
        products: list[LeasewebProduct] = []
        offset = 0
        while True:
            page_params = dict(params)
            page_params.update({"limit": 100, "offset": offset})
            payload = await self._request(
                "GET",
                "/ordering/v1/products/vps",
                params=page_params,
            )
            items = _as_list(payload, "vpss", "products", "data", "items")
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_location = item.get("location")
                if isinstance(raw_location, str) and raw_location.strip():
                    code = raw_location.strip().upper()
                else:
                    code = default_location
                parsed = _parse_product(item, code)
                if parsed is not None:
                    products.append(parsed)
            meta = payload.get("_metadata", {}) if isinstance(payload, dict) else {}
            total = meta.get("totalCount")
            if not isinstance(total, int) or len(products) >= total or not items:
                return products
            offset += len(items)

    async def list_products(self, location: str) -> list[LeasewebProduct]:
        """``GET /ordering/v1/products/vps?location=`` (paginated)."""
        return await self._list_products({"location": location}, location)

    async def list_products_unscoped(self) -> list[LeasewebProduct]:
        """``GET /ordering/v1/products/vps`` WITHOUT a location.

        The official OpenAPI marks ``location`` optional. Best-effort
        discovery helper: whatever the account may see unscoped is
        harvested for candidate locations and doubles as an authentication
        liveness signal. Callers must still probe each location —
        eligibility is only ever decided per location.
        """
        return await self._list_products({}, "")

    async def probe_location(self, location: str) -> LocationProbe:
        """Read-only eligibility probe for one candidate location.

        Performs exactly one catalog read and classifies the outcome.
        Expected account exclusions (403) are reported at INFO level;
        only authentication failures and unexpected errors escalate.
        This method never POSTs anything.
        """
        code = (location or "").strip().upper()
        if not code:
            return LocationProbe("", LocationEligibility.TRANSIENT_UNKNOWN, (), (), "empty code")
        try:
            products = await self.list_products(code)
        except Exception as exc:
            eligibility, note = classify_location_error(exc)
            discovered: tuple[str, ...] = ()
            if isinstance(exc, LeasewebForbiddenError):
                discovered = extract_location_codes(str(exc))
            if eligibility is LocationEligibility.FATAL_AUTHENTICATION:
                logger.error("leaseweb ordering authentication failed while probing %s", code)
            elif eligibility is LocationEligibility.INELIGIBLE_ACCOUNT:
                logger.info("leaseweb location %s not eligible for this account (%s)", code, note)
            else:
                logger.warning("leaseweb location %s probe inconclusive: %s", code, note)
            return LocationProbe(code, eligibility, (), discovered, note)
        if not products:
            return LocationProbe(code, LocationEligibility.ELIGIBLE_EMPTY, (), (), "catalog empty")
        return LocationProbe(
            code,
            LocationEligibility.ELIGIBLE_AVAILABLE,
            tuple(products),
            (),
            f"{len(products)} products",
        )

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
            raise LeasewebAmbiguousMutationError(
                "leaseweb order POST returned an unexpected payload; outcome unknown"
            )
        order_id = payload.get("orderId")
        if order_id is None:
            raise LeasewebAmbiguousMutationError(
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

    async def verify_credential(
        self, candidate: str, *, candidates: Iterable[str] | None = None
    ) -> None:
        """Read-only authentication check with a CANDIDATE key.

        A location-LESS ordering read is NOT a valid authentication probe in
        real Leaseweb production: the unscoped catalog may be refused while
        every location-scoped read for the same key succeeds. The credential
        is therefore judged by SCOPED reads against a bounded candidate set,
        and the outcome is classified:

        * any successful scoped read -> the credential authenticates;
        * 403/404/422 on a location -> the account simply may not order there
          (an eligibility fact, NOT an invalid credential);
        * 401 on a scoped read -> invalid credential;
        * nothing conclusive (transport/5xx/timeout) -> indeterminate, raised
          as an unavailable/transient error, NEVER as an invalid credential.

        The candidate is sent only for these requests; the live key is
        untouched and neither value is ever logged.
        """
        locations = [code for code in (candidates or self.discovery_seeds) if code]
        if not locations:
            locations = list(KNOWN_VPS_DATACENTERS)
        conclusive = False
        inconclusive_error: LeasewebError | None = None
        for location in locations[:_MAX_VERIFY_LOCATIONS]:
            try:
                response = await self._transport.request_raw(
                    "GET",
                    "/ordering/v1/products/vps",
                    params={"location": location, "limit": 1},
                    headers={"X-LSW-Auth": candidate},
                )
                self._raise_for_status(response)
            except LeasewebAuthenticationError:
                raise
            except (
                LeasewebForbiddenError,
                LeasewebNotFoundError,
                LeasewebValidationError,
            ):
                # The endpoint ANSWERED about this location: the credential
                # itself is accepted, it just may not order here.
                conclusive = True
                continue
            except LeasewebError as exc:
                inconclusive_error = exc
                continue
            conclusive = True
            break
        if conclusive:
            return
        if inconclusive_error is not None:
            raise inconclusive_error
        raise LeasewebUnavailableError(
            "credential probe was inconclusive for every candidate location"
        )

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, path: str, *, mutating: bool = False, **kwargs: Any
    ) -> Any:
        """One request through the ONE shared Leaseweb transport.

        The transport owns metrics instrumentation, throttling, credential
        resolution, bounded read retries, structured error mapping and the
        conservative mutation-outcome classification (see
        :class:`~cloud_platform.providers.leaseweb.transport.LeasewebTransport`).

        Read-only requests may be retried on a 429 after the documented
        pause. A MUTATING request is never re-sent here: a mutating 429, a
        5xx after transmission or a mid-flight transport failure all raise
        ``ProviderOutcomeUnknown`` (this adapter only ever mutates the
        billable order POST).
        """
        return await self._transport.request(method, path, mutating=mutating, **kwargs)

    @staticmethod
    def _raise_for_status(response: Any, *, mutating: bool = False) -> Any:
        """Map a raw response onto the Leaseweb error hierarchy.

        Kept as a public-ish helper for credential verification and for
        callers that already hold a response. The transport path uses
        :func:`~cloud_platform.providers.leaseweb.errors.error_for_response`
        (same mapping, including the conservative mutating classification:
        a mutating 429/5xx is an UNKNOWN outcome, never a retry).
        """
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        payload = parse_error_payload(response)
        if mutating and (payload.http_status == 429 or payload.http_status >= 500):
            raise LeasewebAmbiguousMutationError(
                f"leaseweb mutating request returned HTTP {payload.http_status}: "
                f"outcome unknown; {payload.summary}",
                payload=payload,
            )
        raise error_for_response(payload)

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

    Back-compat delegate to the shared transport classifier: a billable
    POST's outcome is UNKNOWN unless the failure provably happened before
    transmission (connect refused/timeout, pool timeout).
    """
    return classify_transport_error(exc) is MutationOutcome.AMBIGUOUS


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
