"""Hetzner hourly pricing and identity parsing (Decimal-exact, fail-closed).

Fixtures are shaped like the official ``GET /server_types`` and ``GET /servers``
payloads, including the envelope fields the platform does not read, so a parser
that silently depends on a flattened mock cannot pass here.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cloud_platform.modules.hourly.service import _response_identity_matches
from cloud_platform.providers.hetzner import hourly as hz


def _price(
    location: str,
    hourly_gross: object,
    monthly_gross: object,
    *,
    declared: bool = True,
    hourly_net: object | None = None,
    monthly_net: object | None = None,
    traffic: object = 21990232555520,
) -> dict[str, object]:
    """One ``prices[]`` entry with the LIVE-VERIFIED key names.

    The provider nests the prices under ``price_hourly``/``price_monthly`` and
    carries ``included_traffic`` on the price entry itself.
    """
    entry: dict[str, object] = {
        "price_hourly": {
            "net": hourly_net if hourly_net is not None else hourly_gross,
            "gross": hourly_gross,
        },
        "price_monthly": {
            "net": monthly_net if monthly_net is not None else monthly_gross,
            "gross": monthly_gross,
        },
        "included_traffic": traffic,
        "price_per_tb_traffic": {"net": "1.0000000000", "gross": "1.0000000000000000"},
    }
    if declared:
        entry["location"] = location
    return entry


_LOCATION_IDS = {"fsn1": 1, "nbg1": 2, "hel1": 3, "ash": 4, "hil": 5, "sin": 6}


def _listed(location: str, *, available: bool = True) -> dict[str, object]:
    """One entry of the server type's own ``locations`` availability block."""
    return {
        "id": _LOCATION_IDS.get(location, 99),
        "name": location,
        "available": available,
        "recommended": False,
        "deprecation": None,
    }


def _server_type(**overrides: object) -> dict[str, object]:
    """One ``/server_types`` entry as Hetzner documents it (cx22)."""
    item: dict[str, object] = {
        "id": 22,
        "name": "cx22",
        "description": "CX22",
        "cores": 2,
        "memory": 4,
        "disk": 40,
        "deprecated": False,
        "deprecation": None,
        "architecture": "x86",
        "cpu_type": "shared",
        "storage_type": "local",
        "category": "cost_optimized",
        "locations": [_listed("fsn1"), _listed("nbg1"), _listed("hel1")],
        "prices": [
            _price("fsn1", "0.0075", "3.92"),
            _price("nbg1", "0.0078", "4.07"),
            _price("hel1", "0.0076", "3.98"),
        ],
    }
    item.update(overrides)
    return item


def _plan(item: dict[str, object], location: str) -> hz.HetznerHourlyPlan:
    parsed = hz.parse_hourly_plan(item, location)
    assert isinstance(parsed, hz.HetznerHourlyPlan), parsed
    return parsed


def _rejection(item: dict[str, object], location: str) -> hz.HetznerHourlyRejection:
    parsed = hz.parse_hourly_plan(item, location)
    assert isinstance(parsed, hz.HetznerHourlyRejection), parsed
    return parsed


# --------------------------------------------------------------------------- pricing


def test_hourly_rate_is_the_provider_hourly_price_not_a_derived_value() -> None:
    plan = _plan(_server_type(), "fsn1")
    assert plan.hourly_rate_exact == "0.0075"
    assert plan.hourly_cost_minor == 1
    # The monthly cap is a SEPARATE provider fact, kept for margin accounting:
    # it is never 0.0075 * 720 and never becomes the hourly rate.
    assert plan.monthly_rate_exact == "3.92"
    assert plan.monthly_cap_minor == 392


def test_sub_cent_hourly_precision_survives_in_the_exact_rate() -> None:
    plan = _plan(_server_type(prices=[_price("fsn1", "0.0063", "3.29")]), "fsn1")
    # Verbatim: the integer minor field cannot hold 0.63 of a cent, so the true
    # rate stays auditable here instead of being rounded away silently.
    assert plan.hourly_rate_exact == "0.0063"
    assert Decimal(plan.hourly_rate_exact) * 100 == Decimal("0.63")
    assert plan.hourly_cost_minor == 1  # 0.63 minor -> HALF_UP -> 1
    assert plan.monthly_cap_minor == 329


def test_each_location_keeps_its_own_provider_price() -> None:
    item = _server_type()
    fsn = _plan(item, "fsn1")
    nbg = _plan(item, "nbg1")
    hel = _plan(item, "hel1")
    assert (fsn.hourly_rate_exact, fsn.monthly_cap_minor) == ("0.0075", 392)
    assert (nbg.hourly_rate_exact, nbg.monthly_cap_minor) == ("0.0078", 407)
    assert (hel.hourly_rate_exact, hel.monthly_cap_minor) == ("0.0076", 398)


def test_missing_hourly_price_is_rejected_rather_than_derived() -> None:
    entry = _price("fsn1", "0.0075", "3.92")
    del entry["price_hourly"]
    rejection = _rejection(_server_type(prices=[entry]), "fsn1")
    # Rejection IS the proof no monthly/720 fallback ran: nothing was published.
    assert rejection.reason == hz.REASON_MISSING_HOURLY
    assert rejection.plan_id == "cx22"


def test_the_two_price_blocks_are_never_conflated() -> None:
    """Rate comes from the hourly block and cap from the monthly one."""
    entry = _price("fsn1", "5.0", "3.92")
    entry["price_hourly"], entry["price_monthly"] = (
        entry["price_monthly"],
        entry["price_hourly"],
    )
    plan = _plan(_server_type(prices=[entry]), "fsn1")
    assert plan.hourly_rate_exact == "3.92"
    assert plan.hourly_cost_minor == 392
    assert plan.monthly_rate_exact == "5"
    assert plan.monthly_cap_minor == 500
    rejection = _rejection(_server_type(prices=[{**entry, "price_monthly": {}}]), "fsn1")
    assert rejection.reason == hz.REASON_MISSING_MONTHLY


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("abc", hz.REASON_MALFORMED_HOURLY),
        ("", hz.REASON_MALFORMED_HOURLY),
        (None, hz.REASON_MISSING_HOURLY),
        (True, hz.REASON_MALFORMED_HOURLY),
        (0.0063, hz.REASON_MALFORMED_HOURLY),  # a float already lost precision
        ("0", hz.REASON_NON_POSITIVE_HOURLY),
        ("-0.1", hz.REASON_NON_POSITIVE_HOURLY),
        ("NaN", hz.REASON_MALFORMED_HOURLY),
        ("Infinity", hz.REASON_MALFORMED_HOURLY),
    ],
)
def test_an_unusable_hourly_value_is_rejected_with_its_reason(bad: object, reason: str) -> None:
    rejection = _rejection(_server_type(prices=[_price("fsn1", bad, "3.92")]), "fsn1")
    assert rejection.reason == reason


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ("abc", hz.REASON_MALFORMED_MONTHLY),
        (None, hz.REASON_MISSING_MONTHLY),
        ("0", hz.REASON_NON_POSITIVE_MONTHLY),
    ],
)
def test_an_unusable_monthly_cap_is_rejected_with_its_reason(bad: object, reason: str) -> None:
    rejection = _rejection(_server_type(prices=[_price("fsn1", "0.0075", bad)]), "fsn1")
    assert rejection.reason == reason


def test_a_deprecated_plan_is_never_published() -> None:
    rejection = _rejection(_server_type(deprecated=True), "fsn1")
    assert rejection.reason == hz.REASON_DEPRECATED


def test_an_entry_without_identity_is_rejected() -> None:
    rejection = _rejection({"prices": [_price("fsn1", "0.0075", "3.92")]}, "fsn1")
    assert rejection.reason == hz.REASON_MISSING_IDENTITY


def test_an_absent_price_block_is_rejected() -> None:
    item = _server_type()
    item.pop("prices")
    assert _rejection(item, "fsn1").reason == hz.REASON_NO_PRICES


def test_an_empty_price_block_is_rejected() -> None:
    assert _rejection(_server_type(prices=[]), "fsn1").reason == hz.REASON_NO_LOCATION_PRICE


def test_an_unsupported_billing_currency_refuses_to_price(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hz, "CURRENCY", "ZZZ")
    rejection = _rejection(_server_type(), "fsn1")
    assert rejection.reason == hz.REASON_UNKNOWN_CURRENCY


# ------------------------------------------------------------- location proof


def test_a_foreign_location_price_is_never_substituted() -> None:
    rejection = _rejection(_server_type(prices=[_price("nbg1", "0.0078", "4.07")]), "fsn1")
    assert rejection.reason == hz.REASON_NO_LOCATION_PRICE


def test_a_single_already_filtered_price_is_accepted() -> None:
    item = _server_type(prices=[_price("fsn1", "0.0075", "3.92", declared=False)])
    assert _plan(item, "fsn1").hourly_rate_exact == "0.0075"


def test_two_undeclared_prices_cannot_prove_a_location() -> None:
    item = _server_type(
        prices=[
            _price("fsn1", "0.0075", "3.92", declared=False),
            _price("nbg1", "0.0078", "4.07", declared=False),
        ]
    )
    assert _rejection(item, "fsn1").reason == hz.REASON_UNPROVEN_LOCATION


def test_a_location_name_field_is_accepted_like_a_location_field() -> None:
    entry = _price("fsn1", "0.0075", "3.92")
    entry["location_name"] = entry.pop("location")
    assert _plan(_server_type(prices=[entry]), "fsn1").hourly_rate_exact == "0.0075"


# ----------------------------------------------------------------- read result


def test_a_read_partitions_priced_plans_from_rejections() -> None:
    read = hz.parse_hourly_plans(
        [
            _server_type(),
            _server_type(id=23, name="cx32", prices=[_price("nbg1", "0.0102", "5.31")]),
        ],
        "fsn1",
    )
    assert read.location_id == "fsn1"
    assert [plan.plan_id for plan in read.plans] == ["cx22"]
    assert [rejection.plan_id for rejection in read.rejected] == ["cx32"]
    assert read.rejected[0].reason == hz.REASON_NO_LOCATION_PRICE
    assert read.rejected[0].location_id == "fsn1"


def test_a_non_list_payload_is_an_empty_read_not_a_crash() -> None:
    read = hz.parse_hourly_plans({"error": {"code": "unauthorized"}}, "fsn1")
    assert read.plans == ()
    assert read.rejected == ()


def test_a_non_dict_entry_is_reported_not_silently_dropped() -> None:
    read = hz.parse_hourly_plans(["nonsense", _server_type()], "fsn1")
    assert [plan.plan_id for plan in read.plans] == ["cx22"]
    assert [rejection.reason for rejection in read.rejected] == [hz.REASON_MISSING_IDENTITY]


# ------------------------------------------------------------ hardware facts


def test_a_plan_carries_the_provider_hardware_and_traffic_facts() -> None:
    plan = _plan(_server_type(memory="4.0"), "fsn1")
    assert (plan.vcpu, plan.ram_gb, plan.disk_gb) == (2, 4, 40)
    assert plan.memory_gb_exact == "4.0"
    assert plan.traffic == "20 TB"
    assert plan.category == "cost_optimized"
    assert plan.architecture == "x86"
    assert (plan.cpu_type, plan.storage_type) == ("shared", "local")
    assert plan.server_type_id == "22"
    assert plan.plan_id == "cx22"
    assert plan.currency == "EUR"
    assert plan.location_id == "fsn1"


def test_an_unknown_architecture_is_dropped_not_displayed() -> None:
    assert _plan(_server_type(architecture="unknown"), "fsn1").architecture is None


def test_minor_units_uses_the_audited_currency_exponent() -> None:
    assert hz.minor_units(Decimal("3.92")) == 392
    assert hz.minor_units(Decimal("0.0050")) == 1  # HALF_UP, never banker's rounding
    assert hz.minor_units(Decimal("0.0049")) == 0


def test_fractional_memory_and_odd_traffic_render_from_decimal_only() -> None:
    plan = _plan(
        _server_type(
            memory="7.5",
            prices=[_price("fsn1", "0.0075", "3.92", traffic=1099511627776)],
        ),
        "fsn1",
    )
    assert (plan.ram_gb, plan.memory_gb_exact) == (8, "7.5")
    assert plan.traffic == "1 TB"


def test_traffic_comes_from_the_price_entry_not_the_server_type() -> None:
    """The real payload carries ``included_traffic`` per location."""
    item = _server_type()
    assert "included_traffic" not in item
    assert _plan(item, "fsn1").traffic == "20 TB"


# ------------------------------------------------- live-verified payload rules


def test_the_documented_price_keys_are_what_we_read() -> None:
    """Regression guard: ``hourly.gross`` does not exist on the real API.

    An entry shaped the old (assumed) way must be REJECTED, not parsed --
    otherwise a flattened fixture would silently reintroduce a parser that
    reads a key the provider never sends.
    """
    item = _server_type(
        prices=[
            {
                "location": "fsn1",
                "hourly": {"gross": "0.9"},
                "monthly": {"gross": "9"},
            }
        ]
    )
    assert _rejection(item, "fsn1").reason == hz.REASON_MISSING_HOURLY


def test_a_plan_priced_but_unavailable_at_the_location_is_rejected() -> None:
    """A price entry is not a licence to sell: availability decides.

    Live example (cx23): priced for hel1 and nbg1 while ``available: false``
    there, so publishing on price alone would create unbuyable offers.
    """
    item = _server_type(locations=[_listed("fsn1", available=False)])
    rejection = _rejection(item, "fsn1")
    assert rejection.reason == hz.REASON_UNAVAILABLE_AT_LOCATION
    assert rejection.plan_id == "cx22"


def test_a_location_the_provider_does_not_list_is_unavailable() -> None:
    item = _server_type(locations=[_listed("fsn1")])
    assert _rejection(item, "nbg1").reason == hz.REASON_UNAVAILABLE_AT_LOCATION


def test_availability_is_not_gated_when_the_payload_states_none() -> None:
    item = _server_type()
    item.pop("locations")
    assert _plan(item, "fsn1").hourly_rate_exact == "0.0075"


def test_an_empty_availability_block_states_nothing() -> None:
    assert _plan(_server_type(locations=[]), "fsn1").hourly_rate_exact == "0.0075"


def test_the_provider_trailing_zero_noise_is_canonicalised() -> None:
    """Live prices arrive as ``"0.0088000000000000"``."""
    plan = _plan(
        _server_type(prices=[_price("fsn1", "0.0088000000000000", "5.4900000000000000")]),
        "fsn1",
    )
    assert plan.hourly_rate_exact == "0.0088"
    assert plan.monthly_rate_exact == "5.49"
    # Canonical spelling, identical number -- no precision was rounded away.
    assert Decimal(plan.hourly_rate_exact) == Decimal("0.0088000000000000")
    assert plan.hourly_cost_minor == 1
    assert plan.monthly_cap_minor == 549


def test_a_deprecation_object_also_marks_the_plan_deprecated() -> None:
    item = _server_type(deprecation={"unavailable_after": "2026-09-01T00:00:00+00:00"})
    assert _rejection(item, "fsn1").reason == hz.REASON_DEPRECATED


# ------------------------------------------------------------------ instances


def _server(**overrides: object) -> dict[str, object]:
    """One ``/servers`` entry as Hetzner documents it."""
    payload: dict[str, object] = {
        "id": 424242,
        "name": "srv-8fc2e573-88a8-4730-b3dd-594f6985ed3f",
        "status": "initializing",
        "datacenter": {
            "id": 2,
            "name": "fsn1-dc14",
            "description": "Falkenstein DC Park 1",
            "location": {"id": 1, "name": "fsn1", "city": "Falkenstein", "country": "DE"},
        },
        "server_type": {"id": 22, "name": "cx22", "cores": 2, "memory": 4.0, "disk": 40},
        "image": {"id": 114690387, "name": "ubuntu-24.04", "os_flavor": "ubuntu"},
        "public_net": {
            "ipv4": {"ip": "192.0.2.10", "blocked": False},
            "ipv6": {"ip": "2001:db8::/64", "blocked": False},
            "floating_ips": [],
        },
    }
    payload.update(overrides)
    return payload


def test_instance_normalization_keeps_lifecycle_and_addresses() -> None:
    instance = hz.parse_hourly_instance(_server())
    assert instance is not None
    assert instance.provider_server_id == "424242"
    assert instance.status == "initializing"  # never assumed "running"
    assert instance.reference == "srv-8fc2e573-88a8-4730-b3dd-594f6985ed3f"
    assert instance.region == "fsn1"
    assert instance.plan_id == "cx22"
    assert instance.image_id == "114690387"
    assert instance.ipv4 == "192.0.2.10"
    assert instance.ipv6 == "2001:db8::/64"
    assert instance.account_id is None


def test_the_dto_satisfies_the_hourly_identity_safety_check() -> None:
    reference = "srv-8fc2e573-88a8-4730-b3dd-594f6985ed3f"
    instance = hz.parse_hourly_instance(_server(name=reference))
    assert instance is not None
    assert instance.name == reference
    assert _response_identity_matches(
        instance,
        location_id="fsn1",
        plan_id="cx22",
        image_id="114690387",
        account_id=None,
        reference=reference,
    )


def test_the_dto_fails_the_identity_check_when_provider_facts_disagree() -> None:
    instance = hz.parse_hourly_instance(_server(name="srv-somebody-elses-server"))
    assert instance is not None
    assert not _response_identity_matches(
        instance,
        location_id="fsn1",
        plan_id="cx22",
        image_id="114690387",
        account_id=None,
        reference="srv-8fc2e573-88a8-4730-b3dd-594f6985ed3f",
    )


def test_the_dto_fails_the_identity_check_at_another_location() -> None:
    instance = hz.parse_hourly_instance(_server())
    assert instance is not None
    assert not _response_identity_matches(
        instance,
        location_id="nbg1",
        plan_id="cx22",
        image_id="114690387",
        account_id=None,
        reference="srv-8fc2e573-88a8-4730-b3dd-594f6985ed3f",
    )


@pytest.mark.parametrize("missing", ["name", "datacenter", "id"])
def test_an_instance_without_provable_identity_is_not_returned(missing: str) -> None:
    payload = _server()
    payload.pop(missing)
    assert hz.parse_hourly_instance(payload) is None


def test_an_instance_without_a_lifecycle_status_still_normalizes() -> None:
    """Identity is what owns a server; an unknown state is not an unowned one."""
    payload = _server()
    payload.pop("status")
    instance = hz.parse_hourly_instance(payload)
    assert instance is not None
    assert instance.status == ""


def test_an_instance_with_a_flattened_location_is_not_trusted() -> None:
    """Only the documented ``datacenter.location`` nest proves the region."""
    payload = _server(location="fsn1")
    payload.pop("datacenter")
    assert hz.parse_hourly_instance(payload) is None


def test_an_instance_without_addresses_still_normalizes() -> None:
    instance = hz.parse_hourly_instance(_server(public_net={}))
    assert instance is not None
    assert (instance.ipv4, instance.ipv6) == (None, None)


def test_an_instance_is_never_read_from_a_non_object_payload() -> None:
    assert hz.parse_hourly_instance(["not", "a", "server"]) is None
