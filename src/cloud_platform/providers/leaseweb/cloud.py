"""Leaseweb hourly Cloud adapter (STOREFRONT-REWORK).

A distinct commercial product from Leaseweb VPS Ordering: usage-based
cloud instances over ``/publicCloud/v1/`` (regions, instance types,
images, instances) with ``contractType=HOURLY`` creation and hourly
customer billing through time accrual.

Deliberately separate from :mod:`cloud_platform.providers.leaseweb.ordering`
(VPS Ordering API) and from the generic single-region port: hourly reads
are region-explicit, prices are
hourly rates parsed from Decimal strings (never float), and creation always
carries the hourly contract. Instance-type families come from the
provider payload when it states them, else the explicit ``other`` family —
the generic UI never invents categories.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.fx.domain import (
    FxPurpose,
    FxUnsupportedCurrencyError,
    major_to_minor,
)
from cloud_platform.modules.offers.domain import HOURLY_MONTHLY_ESTIMATE_HOURS
from cloud_platform.providers.errors import (
    ProviderError,
    ProviderNotFound,
)
from cloud_platform.providers.leaseweb.transport import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    LeasewebTransport,
    Throttle,
)

logger = logging.getLogger(__name__)

#: Provider key served by this adapter (same Leaseweb account universe as
#: VPS ordering; the commercial product differs, not the provider).
PROVIDER_KEY = "leaseweb"

#: Hourly contract marker sent on every instance create.
CONTRACT_TYPE_HOURLY = "HOURLY"

#: Fallback instance family when the provider states no usable category.
OTHER_FAMILY_KEY = "other"
OTHER_FAMILY_NAME = "Other"

#: Monthly estimate convention (display only): hourly rate x 730.
#: Re-exported from the offers domain so sync-time labels and UI math share
#: one value.
HOURS_PER_MONTH_ESTIMATE = HOURLY_MONTHLY_ESTIMATE_HOURS


def _decimal(value: Any) -> Decimal | None:
    """Parse a provider value into Decimal (never float arithmetic)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _price_present(value: Any) -> bool:
    """True when a provider value is a usable positive price in major units.

    Currency-independent on purpose: "this response carries a price" is a
    different fact from "this price is convertible in the envelope
    currency", and the diagnostics must be able to tell them apart.
    """
    parsed = _decimal(value)
    return parsed is not None and parsed > 0


def _provider_minor(value: Any, currency: str) -> int | None:
    """Provider major-unit price -> integer minor units for ``currency``.

    The provider's native currency owns its minor-unit exponent: JPY/KRW are
    zero-decimal, EUR/USD two-decimal. Conversion uses the canonical audited
    money helper (``major_to_minor`` with DISPLAY rounding = HALF_UP), which
    is exactly the rule ``CatalogOfferPricer.price_auto`` re-derives from the
    verbatim provider rate — so sub-cent rates keep the same HALF_UP
    semantic as before while zero-decimal currencies stop being inflated by
    a hardcoded ``* 100``. A currency whose exponent is not audited fails
    closed (``None``) rather than assuming two decimals.
    """
    parsed = _decimal(value)
    if parsed is None or parsed <= 0:
        return None
    try:
        return major_to_minor(parsed, currency, FxPurpose.DISPLAY)
    except (FxUnsupportedCurrencyError, ValueError):
        return None


def _monthly_estimate_minor(hourly_minor: int) -> int:
    """Display-only monthly equivalent of an hourly minor-unit rate."""
    return hourly_minor * HOURS_PER_MONTH_ESTIMATE


def _normalize_country(value: Any) -> str | None:
    """ISO country from the provider payload (None when not a 2-letter code)."""
    code = str(value or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return None
    return code


def _slug(value: str) -> str:
    """Family key from a provider category label (callback-safe)."""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.strip().lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or OTHER_FAMILY_KEY


#: Documented Leaseweb Public Cloud naming taxonomy (adapter-local; the
#: generic storefront must never inspect these prefixes):
#: https://kb.leaseweb.com/kb/public-cloud-new/new-leaseweb-public-cloud/
#: ``lsw.m*`` General Purpose, ``lsw.c*`` Compute Optimized,
#: ``lsw.r*`` Memory Optimized, ``lsw.g*``/``lsw.gr*`` GPU Optimized.
#: The letter after the family initial is always a platform digit (m4i, c6a,
#: r5, g6), so a bare ``lsw.mini``-style word never collides.
_LEASEWEB_FAMILY_BY_PREFIX: tuple[tuple[str, tuple[str, str]], ...] = (
    ("lsw.gr", ("gpu", "GPU Optimized")),
    ("lsw.c", ("compute", "Compute Optimized")),
    ("lsw.m", ("general", "General Purpose")),
    ("lsw.r", ("memory", "Memory Optimized")),
    ("lsw.g", ("gpu", "GPU Optimized")),
)


def classify_instance_family(item: dict[str, Any]) -> tuple[str, str]:
    """(family_key, family_name) for one instance-type payload.

    Uses the provider's own category fields when it states one, then the
    documented ``lsw.*`` naming taxonomy, otherwise the explicit ``other``
    family so unclassifiable types stay visible instead of being silently
    dropped. Never guesses from marketing names.
    """
    for category_field in ("family", "category", "planFamily", "series"):
        raw = item.get(category_field)
        if isinstance(raw, str) and raw.strip():
            return _slug(raw), raw.strip()
    name = str(item.get("name") or "").strip().lower()
    for prefix, family in _LEASEWEB_FAMILY_BY_PREFIX:
        rest = name[len(prefix) :] if name.startswith(prefix) else None
        if rest is not None and (rest == "" or rest[:1] == "." or rest[:1].isdigit()):
            return family
    return OTHER_FAMILY_KEY, OTHER_FAMILY_NAME


@dataclass(frozen=True, slots=True)
class CloudRegion:
    """One hourly-cloud region (provider payload, normalized)."""

    id: str
    name: str
    country_code: str | None
    city: str | None


@dataclass(frozen=True, slots=True)
class CloudInstanceType:
    """One hourly instance type at one region (list payload, normalized).

    ``hourly_cost_minor`` is the provider rate in integer minor units of the
    provider's own currency, converted with that currency's audited exponent
    (HALF_UP, the canonical DISPLAY rounding); ``hourly_rate_exact``
    preserves the provider's verbatim decimal rate (e.g. ``"0.0395"``) so
    sub-cent precision is never silently lost — downstream integer money
    math stays exact while margin audit keeps the true rate.
    ``memory_gb_exact``/network/storage facts preserve provider values that
    do not fit the legacy coarse integer fields.
    """

    id: str
    name: str
    region: str
    family_key: str
    family_name: str
    vcpu: int
    ram_gb: int
    disk_gb: int
    traffic: str | None
    hourly_cost_minor: int
    currency: str
    architecture: str | None = None
    cpu_type: str | None = None
    storage_type: str | None = None
    ipv4: bool | None = None
    ipv6: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    hourly_rate_exact: str = ""
    monthly_cost_minor: int | None = None
    memory_gb_exact: str | None = None
    network_public: str | None = None
    network_private: str | None = None
    storage_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CloudImage:
    """One installable image (label and provider id stay separate)."""

    id: str
    label: str
    os_family: str
    architecture: str | None = None


@dataclass(frozen=True, slots=True)
class CloudRegionsRead:
    """One ``/regions`` read: parsed regions plus the raw envelope size.

    The parsers are lenient by design (unknown envelopes parse to nothing),
    so ``raw_items > 0`` with zero parsed regions is the observable signal
    of schema drift — not of an empty catalog. Diagnostics only; the sync
    path keeps using :meth:`LeasewebHourlyCloudProvider.list_regions`.
    """

    regions: tuple[CloudRegion, ...]
    raw_items: int


@dataclass(frozen=True, slots=True)
class CloudInstanceTypesRead:
    """One ``/instanceTypes`` read: parsed/priced types plus raw size.

    ``priced_items`` counts raw entries carrying a usable hourly price:
    entries present but unpriced are a pricing fact ("no sellable types"),
    not a schema failure. ``currency`` is the envelope ``_metadata.currency``
    of THIS response (None when missing/invalid — pricing fails closed);
    it is never inferred from region, account or country.
    """

    types: tuple[CloudInstanceType, ...]
    raw_items: int
    priced_items: int
    currency: str | None = None
    currency_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class CloudInstance:
    """One hourly instance as the provider reports it.

    Identity fields (type/image/account) are populated only when the API
    response carries them; the hourly service correlates creates through
    them, and an absent field simply cannot prove a match (fail closed).
    """

    id: str
    reference: str
    state: str
    region: str
    ipv4: str | None = None
    ipv6: str | None = None
    instance_type: str | None = None
    image_id: str | None = None
    account_id: str | None = None


def _memory_gb(item: dict[str, Any], resources: dict[str, Any]) -> int:
    for key in ("memoryGb", "memoryMB", "memoryMb", "memory"):
        raw = item.get(key, resources.get(key))
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            continue
        if key.lower() == "memorymb":
            value = value / 1024
        return int(value.to_integral_value(rounding=ROUND_HALF_UP))
    return 0


def _normalize_currency(value: Any) -> str | None:
    """ISO 4217 code from a provider value (None when not a 3-letter code)."""
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        return None
    return code


def _resource_amount(resources: dict[str, Any], *keys: str) -> tuple[Decimal | None, str | None]:
    """Nested ``{value, unit}`` resource facts (official ``resources.*`` shape).

    Returns the exact Decimal value plus its unit; no float, no coercion —
    callers decide how to represent it.
    """
    for key in keys:
        raw = resources.get(key)
        if not isinstance(raw, dict):
            continue
        value = _decimal(raw.get("value"))
        if value is None:
            continue
        unit = raw.get("unit")
        return value, str(unit).strip() if isinstance(unit, str) and unit.strip() else None
    return None, None


def _display_amount(value: Decimal, unit: str | None) -> str:
    """Exact provider value with its unit (``1 Gbps``, ``15.25 GiB``)."""
    text = format(value.normalize(), "f")
    return f"{text} {unit}" if unit else text


def _decimal_string(value: Any) -> str | None:
    """Verbatim decimal text of a provider value (None when not a number)."""
    parsed = _decimal(value)
    if parsed is None:
        return None
    return format(parsed, "f")


def _int_of(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (ValueError, TypeError):
        return 0


#: Leaseweb Public Cloud region geography (adapter-local fallback).
#:
#: The ``/regions`` payload carries ids without country/city, so the generic
#: storefront would render them flagless. Payload fields stay authoritative
#: when present; this table only fills the gap. Sources: the region ids are
#: the provider's documented set (terraform ``public_cloud_instance``);
#: cities follow Leaseweb's datacenter geography (AMS/FRA/LON/MTL/WDC/SFO/
#: TYO/SIN DC pages) and the Public Cloud KB (eu-west-3 availability zones
#: on the NL platform). Regions without solid evidence stay unmapped and
#: render neutrally — a missing flag beats a wrong one.
CLOUD_REGION_DISPLAY: dict[str, tuple[str, str]] = {
    "eu-central-1": ("DE", "Frankfurt"),
    "eu-west-2": ("GB", "London"),
    "eu-west-3": ("NL", "Amsterdam"),
    "us-east-1": ("US", "Washington"),
    "us-west-1": ("US", "San Francisco"),
    "ca-central-1": ("CA", "Montreal"),
    "ap-southeast-1": ("SG", "Singapore"),
    "ap-northeast-1": ("JP", "Tokyo"),
}


def _parse_region(item: dict[str, Any]) -> CloudRegion | None:
    code = str(item.get("name") or item.get("id") or item.get("code") or "").strip()
    if not code:
        return None
    city = item.get("city")
    country = _normalize_country(item.get("country") or item.get("countryCode"))
    city_name = str(city).strip() if isinstance(city, str) and city.strip() else None
    if country is None or city_name is None:
        fallback = CLOUD_REGION_DISPLAY.get(code)
        if fallback is not None:
            fallback_country, fallback_city = fallback
            country = country or fallback_country
            city_name = city_name or fallback_city
    return CloudRegion(
        id=code,
        name=str(item.get("displayName") or item.get("name") or code),
        country_code=country,
        city=city_name,
    )


def _parse_instance_type(
    item: dict[str, Any], region: str, currency: str | None
) -> CloudInstanceType | None:
    """One official ``instanceTypes`` entry (fail closed without pricing).

    Hourly cost comes ONLY from ``prices.hourly`` (legacy ``pricePerHour``
    tolerated with documented precedence); currency comes ONLY from the
    envelope ``_metadata.currency`` passed in — never inferred, never
    defaulted. A missing/invalid rate or currency drops the item.
    """
    raw_id = str(item.get("name") or item.get("id") or "").strip()
    if not raw_id:
        return None
    # Currency is normalized FIRST: it owns the minor-unit exponent every
    # monetary field below is converted with.
    code = _normalize_currency(currency)
    if code is None:
        return None
    raw_money = item.get("prices")
    money: dict[str, Any] = raw_money if isinstance(raw_money, dict) else {}
    hourly_raw = money.get("hourly", item.get("pricePerHour", item.get("price_per_hour")))
    hourly_text = _decimal_string(hourly_raw)
    hourly = _provider_minor(hourly_raw, code)
    if hourly is None or hourly_text is None:
        return None
    monthly_minor = _provider_minor(money.get("monthly", item.get("pricePerMonth")), code)
    raw = item.get("resources")
    resources: dict[str, Any] = raw if isinstance(raw, dict) else {}
    cpu_value, _cpu_unit = _resource_amount(resources, "cpu")
    vcpu = (
        int(cpu_value.to_integral_value(rounding=ROUND_HALF_UP))
        if cpu_value is not None
        else _int_of(item.get("cpu") or item.get("vcpus") or resources.get("cpu"))
    )
    memory_value, memory_unit = _resource_amount(resources, "memory")
    if memory_value is not None:
        ram_gb = int(memory_value.to_integral_value(rounding=ROUND_HALF_UP))
        memory_exact: str | None = _display_amount(memory_value, memory_unit or "GiB")
    else:
        ram_gb = _memory_gb(item, resources)
        memory_exact = None
    public_value, public_unit = _resource_amount(resources, "publicNetworkSpeed")
    private_value, private_unit = _resource_amount(resources, "privateNetworkSpeed")
    storage_list = item.get("storageTypes")
    storage_types = (
        tuple(str(entry).strip() for entry in storage_list if str(entry).strip())
        if isinstance(storage_list, list)
        else ()
    )
    legacy_storage = item.get("storageType")
    storage_type = (
        storage_types[0]
        if storage_types
        else (
            str(legacy_storage).strip()
            if isinstance(legacy_storage, str) and legacy_storage.strip()
            else None
        )
    )
    architecture = str(item.get("architecture") or "").strip() or None
    cpu_type = item.get("cpuType")
    family_key, family_name = classify_instance_family(item)
    traffic = item.get("traffic")
    return CloudInstanceType(
        id=raw_id,
        name=str(item.get("displayName") or raw_id),
        region=region,
        family_key=family_key,
        family_name=family_name,
        vcpu=vcpu,
        ram_gb=ram_gb,
        disk_gb=_int_of(
            item.get("minDiskSize")
            or item.get("rootDiskSize")
            or resources.get("disk")
            or item.get("disk")
        ),
        traffic=str(traffic).strip() if isinstance(traffic, str) and traffic.strip() else None,
        hourly_cost_minor=hourly,
        currency=code,
        architecture=architecture,
        cpu_type=str(cpu_type).strip() if isinstance(cpu_type, str) and cpu_type.strip() else None,
        storage_type=storage_type,
        hourly_rate_exact=hourly_text,
        monthly_cost_minor=monthly_minor,
        memory_gb_exact=memory_exact,
        network_public=_display_amount(public_value, public_unit or "Gbps")
        if public_value is not None
        else None,
        network_private=_display_amount(private_value, private_unit or "Mbps")
        if private_value is not None
        else None,
        storage_types=storage_types,
    )


def _parse_image(item: dict[str, Any]) -> CloudImage | None:
    raw_id = str(item.get("id") or item.get("name") or "").strip()
    if not raw_id:
        return None
    architecture = item.get("architecture")
    return CloudImage(
        id=raw_id,
        label=str(item.get("displayName") or item.get("name") or raw_id),
        os_family=str(item.get("os") or item.get("family") or "unknown"),
        architecture=str(architecture).strip() if architecture else None,
    )


def _parse_instance(item: dict[str, Any]) -> CloudInstance | None:
    raw_id = str(item.get("id") or "").strip()
    if not raw_id:
        return None
    ips = item.get("ipAddresses") or item.get("ips") or []
    ipv4: str | None = None
    ipv6: str | None = None
    if isinstance(ips, list):
        for entry in ips:
            if not isinstance(entry, dict):
                continue
            address = str(entry.get("ip") or entry.get("address") or "")
            version = str(entry.get("version") or "")
            if ":" in address and ipv6 is None:
                ipv6 = address
            elif address and version == "4" and ipv4 is None:
                ipv4 = address
            elif address and ipv4 is None and ":" not in address:
                ipv4 = address

    def _optional(*keys: str) -> str | None:
        for key in keys:
            raw = item.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        return None

    return CloudInstance(
        id=raw_id,
        reference=str(item.get("reference") or item.get("name") or raw_id),
        state=str(item.get("state") or item.get("status") or "unknown"),
        region=str(item.get("region") or ""),
        ipv4=ipv4,
        ipv6=ipv6,
        instance_type=_optional("instanceType", "instance_type", "type"),
        image_id=_optional("imageId", "image_id", "image"),
        account_id=_optional("accountId", "account_id", "salesOrg", "sales_org"),
    )


def build_create_body(
    *,
    instance_type: str,
    image_id: str,
    region: str,
    reference: str,
    ssh_key_id: str | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Exact POST body for an hourly instance (no mutation, preview-safe).

    ``contractType`` is always hourly: this adapter never creates monthly
    contracts. Root disk follows the provider default (the type's disk is
    shown, never invented as a create parameter).
    """
    body: dict[str, Any] = {
        "type": instance_type,
        "imageId": image_id,
        "region": region,
        "reference": reference[:64],
        "contractType": CONTRACT_TYPE_HOURLY,
    }
    if ssh_key_id:
        body["sshKey"] = ssh_key_id
    merged = dict(labels or {})
    body["labels"] = merged
    return body


def hourly_provider_from_settings(settings: Any) -> LeasewebHourlyCloudProvider | None:
    """Build the hourly cloud adapter from settings (shared construction).

    Uses the default API key when set, else the first configured credential
    account's key (hourly regions are account-scoped like ordering
    locations). Returns None when no credential exists — callers skip the
    hourly product instead of failing.
    """
    api_key = (getattr(settings, "leaseweb_api_key", "") or "").strip()
    if not api_key:
        for account in getattr(settings, "leaseweb_accounts", None) or []:
            candidate = (getattr(account, "api_key", "") or "").strip()
            if candidate:
                api_key = candidate
                break
    if not api_key:
        return None
    return LeasewebHourlyCloudProvider(
        api_key=api_key,
        base_url=getattr(settings, "leaseweb_api_base_url", None) or DEFAULT_BASE_URL,
        timeout_seconds=float(getattr(settings, "leaseweb_timeout_seconds", 0) or 30.0),
    )


class LeasewebHourlyCloudProvider:
    """Hourly cloud instances over ``/publicCloud/v1/`` (distinct product).

    Region-explicit reads; creation is hourly-contract-only. Mutations follow
    the platform operation discipline: callers claim the ledger first, and
    ambiguous outcomes reconcile through read-only search (never blind
    re-POSTs — the adapter additionally correlates by deterministic
    reference before creating).
    """

    key = PROVIDER_KEY

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        throttle: Throttle | None = None,
    ) -> None:
        self._transport = LeasewebTransport(
            api_key,
            base_url,
            timeout_seconds=timeout_seconds,
            throttle=throttle,
            provider_key=self.key,
        )

    async def close(self) -> None:
        await self._transport.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._transport.request("GET", path, params=params or {})

    @staticmethod
    def _items(payload: Any, *keys: str) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in keys:
                items = payload.get(key)
                if isinstance(items, list):
                    return items
        return []

    async def read_regions(self) -> CloudRegionsRead:
        """``GET /publicCloud/v1/regions`` with the raw envelope size."""
        payload = await self._get("/publicCloud/v1/regions")
        items = [
            item
            for item in self._items(payload, "regions", "data", "items")
            if isinstance(item, dict)
        ]
        regions = tuple(
            region for region in (_parse_region(item) for item in items) if region is not None
        )
        return CloudRegionsRead(regions=regions, raw_items=len(items))

    async def list_regions(self) -> list[CloudRegion]:
        """``GET /publicCloud/v1/regions`` (authoritative region list)."""
        return list((await self.read_regions()).regions)

    async def read_instance_types(self, region: str) -> CloudInstanceTypesRead:
        """``GET /publicCloud/v1/instanceTypes?region=`` with raw/priced sizes.

        Currency is read ONCE from this response's ``_metadata`` and handed
        to every item parse — per-item currency is never consulted, and a
        missing/invalid envelope currency fails the whole read closed for
        pricing (items still count as raw, so diagnostics can tell "no
        currency" apart from "no types").
        """
        payload = await self._get("/publicCloud/v1/instanceTypes", {"region": region})
        envelope = payload.get("_metadata") if isinstance(payload, dict) else None
        envelope = envelope if isinstance(envelope, dict) else {}
        currency = _normalize_currency(envelope.get("currency"))
        symbol = envelope.get("currencySymbol")
        currency_symbol = (
            str(symbol).strip() if isinstance(symbol, str) and symbol.strip() else None
        )
        items = [
            item
            for item in self._items(payload, "instanceTypes", "types", "data", "items")
            if isinstance(item, dict)
        ]
        parsed = tuple(
            entry for entry in (_parse_instance_type(item, region, currency) for item in items)
        )
        priced = sum(
            1
            for item in items
            if _price_present(
                (item.get("prices") or {}).get("hourly")
                if isinstance(item.get("prices"), dict)
                else item.get("pricePerHour", item.get("price_per_hour"))
            )
        )
        types = tuple(entry for entry in parsed if entry is not None)
        if priced and not types and currency is not None:
            # Diagnosable, non-silent drop: the response carries prices, but
            # the envelope currency has no audited minor-unit exponent, so
            # every conversion fails closed instead of assuming cents.
            logger.warning(
                "leaseweb public cloud region %s: %d priced instance type(s) dropped "
                "because currency %s has no audited minor-unit exponent",
                region,
                priced,
                currency,
            )
        return CloudInstanceTypesRead(
            types=types,
            raw_items=len(items),
            priced_items=priced,
            currency=currency,
            currency_symbol=currency_symbol,
        )

    async def list_instance_types(self, region: str) -> list[CloudInstanceType]:
        """``GET /publicCloud/v1/instanceTypes?region=`` (catalog membership)."""
        return list((await self.read_instance_types(region)).types)

    async def list_images(self, region: str) -> list[CloudImage]:
        """``GET /publicCloud/v1/images?region=`` (live, for the image screen)."""
        payload = await self._get("/publicCloud/v1/images", {"region": region})
        return [
            parsed
            for item in self._items(payload, "images", "data", "items")
            if isinstance(item, dict) and (parsed := _parse_image(item)) is not None
        ]

    async def validate_hourly_offer_for_checkout(
        self,
        *,
        location_id: str,
        product_id: str,
        image_id: str,
        expected_cost_minor: int,
        currency: str,
        expected_cost_exact: str,
    ) -> CloudInstanceType:
        """Revalidate an offer+image against live provider facts (read-only).

        Checkout-time guard required by the hourly service: the pinned type
        must still exist at the region with the same provider cost (minor
        units AND verbatim exact rate) and currency, and the image must be
        listed for the region. Anything else fails closed without mutating.
        """
        types = await self.list_instance_types(location_id)
        match = next((item for item in types if item.id == product_id), None)
        if match is None:
            raise ProviderNotFound(
                f"instance type {product_id!r} is not offered in {location_id!r}"
            )
        if match.currency.upper() != str(currency or "").strip().upper():
            raise ProviderError(
                f"provider cost currency changed for {product_id!r} in {location_id!r}"
            )
        if match.hourly_cost_minor != expected_cost_minor:
            raise ProviderError(f"provider cost changed for {product_id!r} in {location_id!r}")
        wanted_exact = str(expected_cost_exact or "").strip()
        if wanted_exact:
            try:
                wanted = Decimal(wanted_exact)
                live_exact = Decimal(match.hourly_rate_exact)
            except (InvalidOperation, ValueError, AttributeError):
                raise ProviderError(
                    f"provider rate for {product_id!r} is not valid Decimal text"
                ) from None
            if not wanted.is_finite() or wanted <= 0 or live_exact != wanted:
                raise ProviderError(
                    f"exact provider rate changed for {product_id!r} in {location_id!r}"
                )
        images = await self.list_images(location_id)
        if not any(image.id == image_id for image in images):
            raise ProviderNotFound(f"image {image_id!r} is not offered in {location_id!r}")
        return match

    async def list_instances(self, region: str) -> list[CloudInstance]:
        """``GET /publicCloud/v1/instances?region=`` (reconciliation reads)."""
        payload = await self._get(
            "/publicCloud/v1/instances", {"region": region, "limit": 100, "offset": 0}
        )
        return [
            parsed
            for item in self._items(payload, "instances", "data", "items")
            if isinstance(item, dict) and (parsed := _parse_instance(item)) is not None
        ]

    async def get_instance(self, instance_id: str) -> CloudInstance | None:
        """``GET /publicCloud/v1/instances/{id}`` (None when absent)."""
        try:
            payload = await self._get(f"/publicCloud/v1/instances/{instance_id}")
        except ProviderNotFound:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("instance"), dict):
            payload = payload["instance"]
        if not isinstance(payload, dict):
            return None
        return _parse_instance(payload)

    async def find_by_reference(self, region: str, reference: str) -> CloudInstance | None:
        """Read-only correlation match for ambiguous creates (never mutates)."""
        try:
            instances = await self.list_instances(region)
        except ProviderNotFound:
            return None
        for instance in instances:
            if instance.reference == reference:
                return instance
        return None

    async def create_instance(
        self,
        *,
        instance_type: str,
        image_id: str,
        region: str,
        reference: str,
        idempotency_key: IdempotencyKey,
        ssh_key_id: str | None = None,
    ) -> CloudInstance:
        """``POST /publicCloud/v1/instances`` with the hourly contract.

        Get-before-create on the deterministic reference: a prior attempt
        that already materialized is returned as-is instead of launching a
        second billable instance. The platform ledger (not the provider)
        owns exactly-once across processes — concurrent callers serialize
        on the claimed operation before reaching here.
        """
        existing = await self.find_by_reference(region, reference)
        if existing is not None:
            return existing
        labels = {"platform-operation": idempotency_key.value[:63]}
        body = build_create_body(
            instance_type=instance_type,
            image_id=image_id,
            region=region,
            reference=reference,
            ssh_key_id=ssh_key_id,
            labels=labels,
        )
        # Mutating call: transport errors/5xx/429 become ambiguous-outcome
        # errors (never retried inside the transport, never a blind re-POST
        # by callers) instead of plain unavailability.
        payload = await self._transport.request(
            "POST", "/publicCloud/v1/instances", json=body, mutating=True
        )
        if isinstance(payload, dict) and isinstance(payload.get("instance"), dict):
            payload = payload["instance"]
        parsed = _parse_instance(payload) if isinstance(payload, dict) else None
        if parsed is None:
            raise ProviderError("launch instance returned an unexpected payload")
        return parsed
