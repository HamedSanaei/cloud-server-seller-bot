"""Hetzner Cloud hourly facts: normalized DTOs and the ONE price parser.

Hetzner sells Cloud servers by the hour with a monthly cap. ``GET /server_types``
reports, per price entry, the provider's exact hourly and monthly prices. Those
are two different facts: the monthly value is the CAP, the hourly value is the
RATE. The platform must never derive one from the other (``monthly / 720``) --
the provider's own hourly price is authoritative.

The payload shape is verified against the live API, not assumed. Each entry in
``prices`` carries ``price_hourly.gross`` / ``price_monthly.gross`` (decimal
STRINGS, serialized with trailing zeros) plus its own ``included_traffic``, and
the server type separately enumerates ``locations`` with an ``available`` flag.
Two consequences the code below depends on:

* a price entry existing for a location does NOT mean the plan can be created
  there -- ``locations[].available`` is the authoritative signal, so a priced but
  unavailable pair is REJECTED rather than published as an unbuyable offer;
* ``included_traffic`` lives on the price entry, not on the server type.

Rounding policy: the exact provider decimal is preserved on the DTO
(``*_rate_exact``, emitted canonically so the provider's trailing-zero noise
does not become precision) while integer ``*_minor`` fields use the currency's
audited exponent (``HALF_UP``) so downstream money math stays exact integer
arithmetic.

Everything here fails CLOSED. An entry whose location price cannot be PROVEN,
whose hourly gross is missing/malformed/non-positive, that is deprecated, or
that the provider marks unavailable yields a REJECTION carrying a reason --
never a plan with a guessed price, and never another location's price.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from cloud_platform.core.idempotency import IdempotencyKey
from cloud_platform.modules.catalog.image_compatibility import image_compatible
from cloud_platform.modules.fx.domain import SUPPORTED_CURRENCIES, currency_exponent
from cloud_platform.providers.errors import (
    ProviderAuthError,
    ProviderCapacityError,
    ProviderConflict,
    ProviderError,
    ProviderNotFound,
    ProviderOutcomeUnknown,
)

#: Hetzner's identity and billing currency. Provider metadata, not prices --
#: all price values are ingested from the API payload (M04-005).
CURRENCY = "EUR"

#: The provider's own price keys on each ``prices[]`` entry (verified live).
HOURLY_PRICE_KEY = "price_hourly"
MONTHLY_PRICE_KEY = "price_monthly"

# Rejection reasons. Stable strings: they are surfaced verbatim in operator
# diagnostics, so an operator can tell "the provider sent no price" apart from
# "we refused to guess one".
REASON_NO_PRICES = "no-prices"
REASON_NO_LOCATION_PRICE = "no-location-price"
REASON_UNPROVEN_LOCATION = "unproven-location"
REASON_MISSING_HOURLY = "missing-hourly"
REASON_MALFORMED_HOURLY = "malformed-hourly"
REASON_NON_POSITIVE_HOURLY = "non-positive-hourly"
REASON_MISSING_MONTHLY = "missing-monthly"
REASON_MALFORMED_MONTHLY = "malformed-monthly"
REASON_NON_POSITIVE_MONTHLY = "non-positive-monthly"
REASON_DEPRECATED = "deprecated"
REASON_UNAVAILABLE_AT_LOCATION = "unavailable-at-location"
REASON_MISSING_IDENTITY = "missing-identity"
REASON_UNKNOWN_CURRENCY = "unknown-currency"


@dataclass(frozen=True, slots=True)
class HetznerHourlyPlan:
    """One (server type, location) hourly catalog fact.

    ``hourly_cost_minor`` is the provider rate in integer minor units of the
    provider's own currency, converted with that currency's audited exponent
    (HALF_UP, the canonical DISPLAY rounding); ``hourly_rate_exact`` preserves
    the provider's decimal rate (e.g. ``"0.0088"``) so sub-cent precision is
    never silently lost -- downstream integer money math stays exact while
    margin audit keeps the true rate.

    ``monthly_cap_minor``/``monthly_rate_exact`` are the provider's monthly CAP
    (Hetzner stops charging at it). They are a COST fact for margin accounting
    and must never be published as a customer price without the configured
    markup/FX policy applied.
    """

    plan_id: str
    """Provider server type NAME (stable, human-meaningful, accepted by the API
    wherever an id is accepted)."""

    server_type_id: str
    """Provider numeric id. Audit only -- never the offer's plan identity."""

    location_id: str
    hourly_rate_exact: str
    hourly_cost_minor: int
    monthly_rate_exact: str
    monthly_cap_minor: int
    currency: str
    vcpu: int
    ram_gb: int
    memory_gb_exact: str
    disk_gb: int
    traffic: str | None = None
    architecture: str | None = None
    cpu_type: str | None = None
    storage_type: str | None = None
    category: str | None = None


@dataclass(frozen=True, slots=True)
class HetznerHourlyRejection:
    """Why one raw entry produced no hourly plan (never a guessed price)."""

    plan_id: str
    location_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class HourlyPlansRead:
    """One ``/server_types`` read for ONE location.

    ``rejected`` is a pricing fact, not a schema failure: entries the provider
    does not price at this location, or prices unusably, or marks unavailable
    are reported with their reason instead of being silently dropped or
    force-published.
    """

    location_id: str
    plans: tuple[HetznerHourlyPlan, ...]
    rejected: tuple[HetznerHourlyRejection, ...]


class HetznerCreatePassword:
    """In-memory one-time provider response value, never serialized or logged."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value: str | None = value

    def reveal(self) -> str | None:
        value, self._value = self._value, None
        return value

    def __repr__(self) -> str:
        return "HetznerCreatePassword(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class HetznerHourlyInstance:
    """One Hetzner Cloud server, normalized (raw JSON never leaves this layer).

    ``region`` and ``reference`` are REQUIRED identity: the hourly service's
    ``_response_identity_matches()`` refuses to treat a create response as
    success unless it proves our own correlation, so both are populated from the
    provider payload or the parse fails. Optional identity (``plan_id``,
    ``image_id``, ``account_id``) is only reported when the provider actually
    returned it -- a defaulted guess would defeat the check.

    ``status`` is the provider's own lifecycle status, verbatim
    (``initializing``/``running``/``off``/...). Callers must not assume
    ``running`` and must not treat power state as billing state.
    """

    provider_server_id: str
    status: str
    reference: str
    region: str
    plan_id: str | None = None
    image_id: str | None = None
    ipv4: str | None = None
    ipv6: str | None = None
    account_id: str | None = None
    # Set only by the immediate POST response. GET /servers cannot retrieve
    # this provider-issued one-time secret. Never include it in persistence.
    create_password: HetznerCreatePassword | None = None
    # Only a verified system-image POST establishes the documented login.
    ssh_username: str | None = None

    @property
    def id(self) -> str:
        return self.provider_server_id

    @property
    def state(self) -> str:
        return self.status

    @property
    def name(self) -> str:
        """Identity alias: the platform-issued server name IS the reference."""
        return self.reference


def minor_units(value: Decimal, currency: str = CURRENCY) -> int:
    """Exact major-unit decimal -> integer minor units (audited exponent).

    ``HALF_UP`` on the currency's real exponent, never a hardcoded *100: a
    currency with a different minor unit would otherwise be scaled wrong.
    """
    factor = 10 ** currency_exponent(currency)
    return int((value * factor).to_integral_value(rounding=ROUND_HALF_UP))


def exact_text(value: Decimal) -> str:
    """Canonical decimal text without the provider's trailing-zero noise.

    Hetzner serializes prices as ``"0.0088000000000000"``. Those trailing zeros
    are formatting, not precision, so the value is emitted canonically
    (``"0.0088"``): the exact number is unchanged, only its spelling.
    """
    return format(value.normalize(), "f")


def _gross_decimal(entry: dict[str, Any], key: str) -> tuple[Decimal | None, str]:
    """Exact ``gross`` decimal from one price entry, or a reason.

    A JSON ``float`` is rejected instead of parsed: the provider sends decimal
    STRINGS, so a float in this position means precision was already lost
    before the value reached us and it cannot be trusted as money.
    """
    value = entry.get(key)
    if not isinstance(value, dict):
        return None, "missing"
    gross = value.get("gross")
    if gross is None:
        return None, "missing"
    if isinstance(gross, (bool, float)):
        return None, "malformed"
    try:
        parsed = Decimal(str(gross))
    except (InvalidOperation, ValueError):
        return None, "malformed"
    if not parsed.is_finite():
        return None, "malformed"
    if parsed <= 0:
        return None, "non-positive"
    return parsed, ""


def _declared_location(entry: dict[str, Any]) -> str:
    return str(entry.get("location") or entry.get("location_name") or "").strip()


def _location_price_entry(
    item: dict[str, Any], location_id: str
) -> tuple[dict[str, Any] | None, str]:
    """The price entry that PROVES this location, or why it cannot be proven.

    Only an entry declaring exactly this location is accepted. A single entry
    declaring NO location is accepted too, because the request itself was
    location-scoped and Hetzner then returns the already-filtered price in that
    shape. An entry declaring a DIFFERENT location is NEVER substituted: a
    neighbouring location's price is not this location's price.
    """
    raw_prices = item.get("prices")
    if not isinstance(raw_prices, list):
        return None, REASON_NO_PRICES
    entries = [raw for raw in raw_prices if isinstance(raw, dict)]
    for entry in entries:
        if _declared_location(entry) == location_id:
            return entry, ""
    undeclared = [entry for entry in entries if not _declared_location(entry)]
    if len(undeclared) == 1:
        return undeclared[0], ""
    if len(undeclared) > 1:
        return None, REASON_UNPROVEN_LOCATION
    return None, REASON_NO_LOCATION_PRICE


def _location_availability(item: dict[str, Any], location_id: str) -> bool | None:
    """``locations[].available`` for this location, as the provider reports it.

    The server type enumerates the locations it can be created in and flags each
    one. A location listed as unavailable is authoritative -- a live example is
    a plan that still carries a price entry for a location it cannot be created
    in, so a priced pair is not by itself a sellable one.

    Returns None only when the payload says nothing about availability at all
    (no ``locations`` block), which is the one case the caller may treat as
    unstated rather than unproven.
    """
    raw_locations = item.get("locations")
    if not isinstance(raw_locations, list) or not raw_locations:
        return None
    for entry in raw_locations:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "").strip() == location_id:
            return bool(entry.get("available", False))
    # The provider enumerates availability: a location it did not list is not
    # one this plan can be created in.
    return False


def _reject(plan_id: str, location_id: str, reason: str) -> HetznerHourlyRejection:
    return HetznerHourlyRejection(plan_id=plan_id, location_id=location_id, reason=reason)


def parse_hourly_plan(
    item: dict[str, Any], location_id: str
) -> HetznerHourlyPlan | HetznerHourlyRejection:
    """Normalize one ``/server_types`` entry into an hourly plan -- or reject it.

    The provider payload is authoritative: whatever it says this location costs
    per hour is what is stored, exactly. Nothing is inferred, defaulted or
    derived from another location or from the monthly value.
    """
    plan_id = str(item.get("name") or item.get("id") or "").strip()
    if not plan_id:
        return _reject("", location_id, REASON_MISSING_IDENTITY)
    if CURRENCY not in SUPPORTED_CURRENCIES:
        return _reject(plan_id, location_id, REASON_UNKNOWN_CURRENCY)
    if bool(item.get("deprecated", False)) or item.get("deprecation"):
        return _reject(plan_id, location_id, REASON_DEPRECATED)
    if _location_availability(item, location_id) is False:
        return _reject(plan_id, location_id, REASON_UNAVAILABLE_AT_LOCATION)

    entry, reason = _location_price_entry(item, location_id)
    if entry is None:
        return _reject(plan_id, location_id, reason)

    hourly, reason = _gross_decimal(entry, HOURLY_PRICE_KEY)
    if hourly is None:
        return _reject(plan_id, location_id, f"{reason}-hourly")
    monthly, reason = _gross_decimal(entry, MONTHLY_PRICE_KEY)
    if monthly is None:
        return _reject(plan_id, location_id, f"{reason}-monthly")

    raw_architecture = str(item.get("architecture") or "").strip()
    architecture = raw_architecture if raw_architecture.lower() != "unknown" else ""
    cpu_type = item.get("cpu_type")
    storage_type = item.get("storage_type")
    category = item.get("category")
    raw_memory = item.get("memory")
    raw_traffic = entry.get("included_traffic")
    if raw_traffic is None:
        raw_traffic = item.get("included_traffic")
    return HetznerHourlyPlan(
        plan_id=plan_id,
        server_type_id=str(item.get("id") or ""),
        location_id=location_id,
        hourly_rate_exact=exact_text(hourly),
        hourly_cost_minor=minor_units(hourly),
        monthly_rate_exact=exact_text(monthly),
        monthly_cap_minor=minor_units(monthly),
        currency=CURRENCY,
        vcpu=int(item.get("cores") or 0),
        ram_gb=memory_gb(raw_memory),
        memory_gb_exact=str(raw_memory) if raw_memory is not None else "",
        disk_gb=int(item.get("disk") or 0),
        traffic=traffic_label(raw_traffic),
        architecture=architecture or None,
        cpu_type=str(cpu_type) if cpu_type else None,
        storage_type=str(storage_type) if storage_type else None,
        category=str(category) if category else None,
    )


def parse_hourly_plans(items: Any, location_id: str) -> HourlyPlansRead:
    """Parse one location-scoped ``/server_types`` payload (fail closed).

    A non-list payload yields an empty read rather than an exception: an
    unparseable envelope is "no sellable plans at this location", and the
    caller's completeness rules decide whether the catalog may be retired.
    """
    plans: list[HetznerHourlyPlan] = []
    rejected: list[HetznerHourlyRejection] = []
    if not isinstance(items, list):
        return HourlyPlansRead(location_id=location_id, plans=(), rejected=())
    for item in items:
        if not isinstance(item, dict):
            rejected.append(_reject("", location_id, REASON_MISSING_IDENTITY))
            continue
        parsed = parse_hourly_plan(item, location_id)
        if isinstance(parsed, HetznerHourlyRejection):
            rejected.append(parsed)
        else:
            plans.append(parsed)
    return HourlyPlansRead(location_id=location_id, plans=tuple(plans), rejected=tuple(rejected))


def parse_hourly_instance(
    payload: Any, *, account_id: str | None = None
) -> HetznerHourlyInstance | None:
    """Normalize one ``/servers`` entry, or None when identity cannot be proven.

    Returning None is the safe answer: a response missing the reference or the
    location can never prove it is OUR server, and the create path treats an
    unproven response as unverified rather than as success.
    """
    if not isinstance(payload, dict):
        return None
    raw_id = payload.get("id")
    if raw_id is None:
        return None
    reference = str(payload.get("name") or "").strip()
    region = _instance_region(payload)
    if not reference or not region:
        return None
    server_type = payload.get("server_type")
    plan_id: str | None = None
    if isinstance(server_type, dict):
        raw_plan = server_type.get("name") or server_type.get("id")
        plan_id = str(raw_plan) if raw_plan is not None else None
    elif isinstance(server_type, str) and server_type.strip():
        plan_id = server_type.strip()
    ipv4, ipv6 = _instance_addresses(payload)
    return HetznerHourlyInstance(
        provider_server_id=str(raw_id),
        status=str(payload.get("status") or "").strip(),
        reference=reference,
        region=region,
        plan_id=plan_id,
        image_id=_image_id(payload.get("image")),
        ipv4=ipv4,
        ipv6=ipv6,
        account_id=account_id,
    )


def _instance_region(payload: dict[str, Any]) -> str:
    """Use Hetzner's location object; reject conflicting location evidence."""
    location = payload.get("location")
    direct = str(location.get("name") or "").strip() if isinstance(location, dict) else ""
    datacenter = payload.get("datacenter")
    nested = ""
    if isinstance(datacenter, dict):
        dc_location = datacenter.get("location")
        if isinstance(dc_location, dict):
            nested = str(dc_location.get("name") or "").strip()
    if direct and nested and direct != nested:
        return ""
    return direct or nested


def _instance_addresses(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    public_net = payload.get("public_net")
    if not isinstance(public_net, dict):
        return None, None
    ipv4 = public_net.get("ipv4")
    ipv4_address = (
        str(ipv4.get("ip")).strip() if isinstance(ipv4, dict) and ipv4.get("ip") else None
    )
    ipv6 = public_net.get("ipv6")
    ipv6_address = (
        str(ipv6.get("ip")).strip() if isinstance(ipv6, dict) and ipv6.get("ip") else None
    )
    return ipv4_address, ipv6_address


def _image_id(image: Any) -> str | None:
    if isinstance(image, dict):
        raw = image.get("id")
        if raw is not None:
            return str(raw)
        name = image.get("name")
        return str(name) if name else None
    if isinstance(image, str) and image.strip():
        return image.strip()
    return None


def memory_gb(value: Any) -> int:
    """Hetzner reports memory in GB as a decimal string ("4.0") -> integer GB."""
    if value is None:
        return 0
    try:
        return int(Decimal(str(value)).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return 0


def traffic_label(value: Any) -> str | None:
    """Included traffic (bytes, provider-reported) -> display label.

    Rendered in binary terabytes, the unit the provider itself uses for
    included traffic, from Decimal arithmetic only.
    """
    if value is None or isinstance(value, (bool, float)):
        return None
    try:
        total = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if total <= 0:
        return None
    terabytes = total / (Decimal(2) ** 40)
    if terabytes == terabytes.to_integral_value():
        return f"{int(terabytes)} TB"
    return f"{terabytes.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)} TB"


@dataclass(frozen=True, slots=True)
class HetznerHourlyLocation:
    id: str
    name: str
    country_code: str | None
    city: str | None


@dataclass(frozen=True, slots=True)
class HetznerHourlyImage:
    id: str
    label: str
    os_family: str
    architecture: str


@dataclass(frozen=True, slots=True)
class HetznerHourlyRootDisk:
    size_gb: int
    storage_type: str


@dataclass(frozen=True, slots=True)
class HetznerHourlyCheckoutFacts:
    instance_type: HetznerHourlyPlan
    root_disk: HetznerHourlyRootDisk


class HetznerHourlyCloudProvider:
    """Application-facing hourly server port; monthly provider is unchanged.

    Hetzner has no generic POST idempotency key: deterministic server name and
    read-only, exhaustively paginated lookup are the recovery identity. The
    caller's durable operation ledger serializes competing creates.
    """

    key = "hetzner"
    issues_password_on_create = True

    def __init__(self, token: str, base_url: str = "https://api.hetzner.cloud/v1") -> None:
        # Deferred import avoids the legacy client -> sync -> hourly import cycle.
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        self._provider = HetznerCloudProvider(token=token, base_url=base_url)

    async def close(self) -> None:
        await self._provider.close()

    async def aclose(self) -> None:
        await self.close()

    async def _pages(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Consume documented meta.pagination.next_page; never search partial data."""
        items: list[dict[str, Any]] = []
        page = 1
        for _ in range(1000):
            payload = await self._provider._request(
                "GET", path, params={**(params or {}), "page": page, "per_page": 50}
            )
            raw = payload.get(key)
            meta = payload.get("meta")
            pagination = meta.get("pagination") if isinstance(meta, dict) else None
            if not isinstance(raw, list) or not isinstance(pagination, dict):
                raise ProviderError(f"{path} returned an incomplete list envelope")
            if any(not isinstance(item, dict) for item in raw):
                raise ProviderError(f"{path} returned an invalid list entry")
            items.extend(raw)
            next_page = pagination.get("next_page")
            if next_page is None:
                return items
            if isinstance(next_page, bool) or not isinstance(next_page, int) or next_page <= page:
                raise ProviderError(f"{path} returned invalid pagination")
            page = next_page
        raise ProviderError(f"{path} exceeded safe pagination limit")

    async def read_locations(self) -> tuple[HetznerHourlyLocation, ...]:
        items = await self._pages("/locations", "locations")
        locations: list[HetznerHourlyLocation] = []
        for item in items:
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ProviderError("location response has no provider name")
            locations.append(
                HetznerHourlyLocation(
                    id=name,
                    name=str(item.get("description") or name),
                    country_code=str(item.get("country") or "") or None,
                    city=str(item.get("city") or "") or None,
                )
            )
        return tuple(locations)

    async def list_locations(self) -> list[HetznerHourlyLocation]:
        return list(await self.read_locations())

    async def read_instance_types(self, location: str) -> HourlyPlansRead:
        items = await self._pages("/server_types", "server_types", {"location": location})
        return parse_hourly_plans(items, location)

    async def list_instance_types(self, location: str) -> list[HetznerHourlyPlan]:
        return list((await self.read_instance_types(location)).plans)

    async def list_images(self, location: str) -> list[HetznerHourlyImage]:
        """System images are global; location feasibility comes from type facts."""
        if not any(item.id == location for item in await self.read_locations()):
            raise ProviderNotFound(f"unknown Hetzner location {location!r}")
        items = await self._pages(
            "/images", "images", {"type": "system", "include_deprecated": "false"}
        )
        images: list[HetznerHourlyImage] = []
        for item in items:
            if (
                item.get("type") != "system"
                or item.get("status") != "available"
                or item.get("deprecated") is True
                or item.get("deprecation")
            ):
                continue
            image_id = item.get("id")
            arch = item.get("architecture")
            if image_id is None or not isinstance(arch, str) or not arch.strip():
                continue
            images.append(
                HetznerHourlyImage(
                    id=str(image_id),
                    label=str(item.get("name") or item.get("description") or image_id),
                    os_family=str(item.get("os_flavor") or "unknown"),
                    architecture=arch.strip(),
                )
            )
        return images

    async def installable_images(self, location: str) -> list[HetznerHourlyImage]:
        return await self.list_images(location)

    async def validate_hourly_offer_for_checkout(
        self,
        *,
        location_id: str,
        product_id: str,
        image_id: str,
        expected_cost_minor: int,
        currency: str,
        expected_cost_exact: str,
        root_disk_size_gb: int | None = None,
        root_disk_storage_type: str | None = None,
    ) -> HetznerHourlyCheckoutFacts:
        if currency != CURRENCY:
            raise ProviderConflict(f"Hetzner hourly currency changed from {currency!r}")
        plan = next(
            (
                item
                for item in await self.list_instance_types(location_id)
                if item.plan_id == product_id
            ),
            None,
        )
        if plan is None:
            raise ProviderNotFound(f"server type {product_id!r} unavailable at {location_id!r}")
        if plan.hourly_cost_minor != expected_cost_minor:
            raise ProviderConflict(f"hourly cost changed for {product_id!r} at {location_id!r}")
        try:
            expected = Decimal(expected_cost_exact)
        except (ValueError, InvalidOperation, TypeError) as exc:
            raise ProviderConflict("invalid pinned exact hourly rate") from exc
        if not expected.is_finite() or expected <= 0 or expected != Decimal(plan.hourly_rate_exact):
            raise ProviderConflict(
                f"exact hourly rate changed for {product_id!r} at {location_id!r}"
            )
        return await self._validate_image_and_disk(
            plan, image_id, location_id, root_disk_size_gb, root_disk_storage_type
        )

    async def _validate_image_and_disk(
        self,
        plan: HetznerHourlyPlan,
        image_id: str,
        location_id: str,
        root_disk_size_gb: int | None,
        root_disk_storage_type: str | None,
    ) -> HetznerHourlyCheckoutFacts:
        image = next(
            (item for item in await self.list_images(location_id) if item.id == image_id),
            None,
        )
        if image is None or not image_compatible(
            image, plan_id=plan.plan_id, architecture=plan.architecture, location_id=location_id
        ):
            raise ProviderNotFound(f"image {image_id!r} incompatible or unavailable")
        if plan.disk_gb <= 0 or not plan.storage_type:
            raise ProviderConflict(f"server type {plan.plan_id!r} lacks a provable root disk")
        disk = HetznerHourlyRootDisk(plan.disk_gb, plan.storage_type.upper())
        if root_disk_size_gb is not None or root_disk_storage_type is not None:
            if (
                root_disk_size_gb != disk.size_gb
                or str(root_disk_storage_type or "").upper() != disk.storage_type
            ):
                raise ProviderConflict("pinned root disk differs from Hetzner server type")
        return HetznerHourlyCheckoutFacts(plan, disk)

    async def get_instance(self, instance_id: str) -> HetznerHourlyInstance | None:
        try:
            payload = await self._provider._request("GET", f"/servers/{instance_id}")
        except ProviderNotFound:
            return None
        instance = parse_hourly_instance(payload.get("server"))
        if instance is None:
            raise ProviderError("GET /servers/{id} returned an incomplete server identity")
        return instance

    async def find_by_reference(self, region: str, reference: str) -> HetznerHourlyInstance | None:
        """A name-filtered exhaustive read, rejecting duplicate or unproven matches."""
        matches: list[HetznerHourlyInstance] = []
        for raw in await self._pages("/servers", "servers", {"name": reference}):
            if raw.get("name") != reference:
                continue
            instance = parse_hourly_instance(raw)
            if instance is None:
                raise ProviderError("reference matched a server without complete identity")
            matches.append(instance)
        if len(matches) > 1:
            raise ProviderConflict(f"multiple Hetzner servers share reference {reference!r}")
        if not matches:
            return None
        if matches[0].region != region:
            raise ProviderConflict("server reference is already used in a different location")
        return matches[0]

    async def create_instance(
        self,
        *,
        instance_type: str,
        image_id: str,
        region: str,
        reference: str,
        root_disk_size_gb: int,
        root_disk_storage_type: str,
        idempotency_key: IdempotencyKey,
        ssh_key_id: str | None = None,
        image_label: str | None = None,
        os_family: str | None = None,
    ) -> HetznerHourlyInstance:
        del idempotency_key, image_label, os_family
        existing = await self.find_by_reference(region, reference)
        if existing is not None:
            if existing.plan_id != instance_type or existing.image_id != image_id:
                raise ProviderConflict("server reference belongs to a different pinned contract")
            return existing
        plan = next(
            (p for p in await self.list_instance_types(region) if p.plan_id == instance_type),
            None,
        )
        if plan is None:
            raise ProviderNotFound(f"server type {instance_type!r} unavailable at {region!r}")
        await self._validate_image_and_disk(
            plan, image_id, region, root_disk_size_gb, root_disk_storage_type
        )
        body: dict[str, Any] = {
            "name": reference,
            "server_type": instance_type,
            "image": image_id,
            "location": region,
        }
        if ssh_key_id is not None:
            body["ssh_keys"] = [ssh_key_id]
        # No _provider._request: its 429 retry is appropriate for GET but not
        # for a billable POST without provider-supported idempotency.
        try:
            response = await self._provider._client.request("POST", "/servers", json=body)
        except httpx.RequestError as exc:
            raise ProviderOutcomeUnknown("Hetzner create transport outcome unknown") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderOutcomeUnknown(
                f"Hetzner create outcome unknown (HTTP {response.status_code})"
            )
        if response.status_code == 422:
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            if isinstance(error, dict) and error.get("code") == "resource_limit_exceeded":
                raise ProviderCapacityError("Hetzner account resource limit exceeded")
        if response.status_code in {401, 403}:
            raise ProviderAuthError(f"Hetzner create refused (HTTP {response.status_code})")
        if response.status_code == 404:
            raise ProviderNotFound("Hetzner create resource not found")
        if response.status_code in {409, 423}:
            raise ProviderConflict(f"Hetzner create conflict (HTTP {response.status_code})")
        if response.is_error:
            raise ProviderError(f"Hetzner create refused (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderOutcomeUnknown("Hetzner create returned invalid JSON") from exc
        instance = parse_hourly_instance(
            payload.get("server") if isinstance(payload, dict) else None
        )
        if (
            instance is None
            or instance.region != region
            or instance.reference != reference
            or instance.plan_id != instance_type
            or instance.image_id != image_id
        ):
            raise ProviderOutcomeUnknown("Hetzner create response identity not proven")
        if ssh_key_id is None:
            # Hetzner returns root_password at the ENVELOPE level, never in
            # `server`; it is absent/null with SSH keys. Do not infer or reset
            # passwords if absent, or trust a response with unproven identity.
            raw_password = payload.get("root_password")
            if isinstance(raw_password, str) and raw_password:
                # list_images accepts Hetzner system images only; the selected
                # image was revalidated above. Hetzner's own connecting guide
                # documents root for these images and calls this root_password.
                instance = replace(
                    instance,
                    create_password=HetznerCreatePassword(raw_password),
                    ssh_username="root",
                )
        return instance

    async def delete_instance(
        self, instance_id: str, idempotency_key: IdempotencyKey | None = None
    ) -> None:
        del idempotency_key
        try:
            response = await self._provider._client.request("DELETE", f"/servers/{instance_id}")
        except httpx.RequestError as exc:
            raise ProviderOutcomeUnknown("Hetzner delete transport outcome unknown") from exc
        if response.status_code == 404:
            return
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderOutcomeUnknown(
                f"Hetzner delete outcome unknown (HTTP {response.status_code})"
            )
        if response.status_code in {401, 403}:
            raise ProviderAuthError(f"Hetzner delete refused (HTTP {response.status_code})")
        if response.status_code in {409, 423}:
            raise ProviderConflict(f"Hetzner delete conflict (HTTP {response.status_code})")
        if response.is_error:
            raise ProviderError(f"Hetzner delete refused (HTTP {response.status_code})")


__all__ = [
    "CURRENCY",
    "HOURLY_PRICE_KEY",
    "MONTHLY_PRICE_KEY",
    "HetznerHourlyCheckoutFacts",
    "HetznerHourlyCloudProvider",
    "HetznerHourlyImage",
    "HetznerHourlyInstance",
    "HetznerHourlyLocation",
    "HetznerHourlyPlan",
    "HetznerHourlyRejection",
    "HetznerHourlyRootDisk",
    "HourlyPlansRead",
    "exact_text",
    "memory_gb",
    "minor_units",
    "parse_hourly_instance",
    "parse_hourly_plan",
    "parse_hourly_plans",
    "traffic_label",
]
