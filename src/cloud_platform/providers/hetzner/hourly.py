"""Hetzner Cloud hourly facts: normalized DTOs and the ONE price parser.

Hetzner sells Cloud servers by the hour with a monthly cap. ``GET /server_types``
reports, per (server type, location), an exact ``hourly.gross`` and an exact
``monthly.gross``. Those are two different provider facts: the monthly value is
the CAP, the hourly value is the RATE. The platform must never derive one from
the other (``monthly / 720``) -- the provider's own hourly price is
authoritative, and its sub-cent precision (``0.0063`` EUR) is real money.

Rounding policy: the exact provider decimal TEXT is preserved verbatim on the
DTO (``*_rate_exact``), so margin audit keeps the true rate even where it has
more precision than the platform's minor unit can express, while integer
``*_minor`` fields are produced with the currency's audited exponent
(``HALF_UP``) so downstream money math stays exact integer arithmetic.

Everything here fails CLOSED. An entry whose location price cannot be PROVEN,
whose hourly gross is missing/malformed/non-positive, or that the provider
marks deprecated yields a REJECTION carrying a reason -- never a plan with a
guessed price, and never another location's price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from cloud_platform.modules.fx.domain import SUPPORTED_CURRENCIES, currency_exponent

#: Hetzner's identity and billing currency. Provider metadata, not prices --
#: all price values are ingested from the API payload (M04-005).
CURRENCY = "EUR"

# Rejection reasons. Stable strings: they are surfaced verbatim in operator
# diagnostics (``hetzner doctor`` / catalog auto-sync doctor), so an operator can
# tell "the provider sent no price" apart from "we refused to guess one".
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
REASON_MISSING_IDENTITY = "missing-identity"
REASON_UNKNOWN_CURRENCY = "unknown-currency"


@dataclass(frozen=True, slots=True)
class HetznerHourlyPlan:
    """One (server type, location) hourly catalog fact.

    ``hourly_cost_minor`` is the provider rate in integer minor units of the
    provider's own currency, converted with that currency's audited exponent
    (HALF_UP, the canonical DISPLAY rounding); ``hourly_rate_exact`` preserves
    the provider's verbatim decimal rate (e.g. ``"0.0063"``) so sub-cent
    precision is never silently lost -- downstream integer money math stays
    exact while margin audit keeps the true rate.

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
    does not price at this location (or prices unusably) are reported with
    their reason instead of being silently dropped or force-published.
    """

    location_id: str
    plans: tuple[HetznerHourlyPlan, ...]
    rejected: tuple[HetznerHourlyRejection, ...]


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


def _gross_decimal(raw: Any, key: str) -> tuple[Decimal | None, str]:
    """Exact ``gross`` decimal from one price entry, or a reason.

    A JSON ``float`` is rejected instead of parsed: the provider sends decimal
    STRINGS, so a float in this position means precision was already lost
    before the value reached us and it cannot be trusted as money.
    """
    if not isinstance(raw, dict):
        return None, "missing"
    value = raw.get(key)
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
    if bool(item.get("deprecated", False)):
        return _reject(plan_id, location_id, REASON_DEPRECATED)

    entry, reason = _location_price_entry(item, location_id)
    if entry is None:
        return _reject(plan_id, location_id, reason)

    hourly, reason = _gross_decimal(entry, "hourly")
    if hourly is None:
        return _reject(plan_id, location_id, f"{reason}-hourly")
    monthly, reason = _gross_decimal(entry, "monthly")
    if monthly is None:
        return _reject(plan_id, location_id, f"{reason}-monthly")

    raw_architecture = str(item.get("architecture") or "").strip()
    architecture = raw_architecture if raw_architecture.lower() != "unknown" else ""
    cpu_type = item.get("cpu_type")
    storage_type = item.get("storage_type")
    raw_memory = item.get("memory")
    return HetznerHourlyPlan(
        plan_id=plan_id,
        server_type_id=str(item.get("id") or ""),
        location_id=location_id,
        hourly_rate_exact=str(hourly),
        hourly_cost_minor=minor_units(hourly),
        monthly_rate_exact=str(monthly),
        monthly_cap_minor=minor_units(monthly),
        currency=CURRENCY,
        vcpu=int(item.get("cores") or 0),
        ram_gb=memory_gb(raw_memory),
        memory_gb_exact=str(raw_memory) if raw_memory is not None else "",
        disk_gb=int(item.get("disk") or 0),
        traffic=traffic_label(item.get("included_traffic")),
        architecture=architecture or None,
        cpu_type=str(cpu_type) if cpu_type else None,
        storage_type=str(storage_type) if storage_type else None,
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
    unproven response as unverified rather than as success (§3).
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
    """The location id from ``datacenter.location`` (never inferred)."""
    datacenter = payload.get("datacenter")
    if not isinstance(datacenter, dict):
        return ""
    location = datacenter.get("location")
    if isinstance(location, dict):
        return str(location.get("name") or "").strip()
    if isinstance(location, str):
        return location.strip()
    return ""


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


__all__ = [
    "CURRENCY",
    "HetznerHourlyInstance",
    "HetznerHourlyPlan",
    "HetznerHourlyRejection",
    "HourlyPlansRead",
    "memory_gb",
    "minor_units",
    "parse_hourly_instance",
    "parse_hourly_plan",
    "parse_hourly_plans",
    "traffic_label",
]
