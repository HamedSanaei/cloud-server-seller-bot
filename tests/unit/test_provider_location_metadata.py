"""Provider location metadata normalization (STOREFRONT-V2).

- Leaseweb ordering discovery has no location-list endpoint, so an
  ordering-discovered code with no exact display entry (FRA-10, FRA-14,
  LON-11, LON-12) resolves through the adapter-internal city-prefix
  fallback — never metadata-less, never guessed outside the adapter.
- Hetzner normalizes its own /locations payload defensively (case,
  whitespace, non-codes become unknown, never a guessed country).
"""

from __future__ import annotations

from cloud_platform.providers.hetzner.sync import _normalize_country
from cloud_platform.providers.leaseweb.ordering import (
    LOCATION_DISPLAY,
    LOCATION_PREFIX_DISPLAY,
    describe_location_code,
)


class TestLeasewebLocationCodes:
    def test_exact_entries_win(self) -> None:
        assert describe_location_code("FRA-01") == (
            "DE",
            "Frankfurt",
            "leaseweb-ordering-discovery",
        )
        assert describe_location_code("LON-01") == (
            "GB",
            "London",
            "leaseweb-ordering-discovery",
        )
        assert describe_location_code("AMS-01") == (
            "NL",
            "Amsterdam",
            "leaseweb-ordering-discovery",
        )

    def test_ordering_only_codes_resolve_by_prefix(self) -> None:
        assert describe_location_code("FRA-10") == ("DE", "Frankfurt", "leaseweb-ordering-prefix")
        assert describe_location_code("FRA-14") == ("DE", "Frankfurt", "leaseweb-ordering-prefix")
        assert describe_location_code("LON-11") == ("GB", "London", "leaseweb-ordering-prefix")
        assert describe_location_code("LON-12") == ("GB", "London", "leaseweb-ordering-prefix")

    def test_codes_are_case_insensitive(self) -> None:
        assert describe_location_code("fra-10")[0] == "DE"
        assert describe_location_code(" lon-11 ")[1] == "London"

    def test_unknown_codes_stay_verbatim_without_country(self) -> None:
        country, city, _source = describe_location_code("XX-99")
        assert country == ""
        assert city == "XX-99"

    def test_every_known_datacenter_resolves(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import KNOWN_VPS_DATACENTERS

        for code in KNOWN_VPS_DATACENTERS:
            country, city, _source = describe_location_code(code)
            assert country, code
            assert city, code

    def test_prefix_table_covers_every_exact_entry(self) -> None:
        for code in LOCATION_DISPLAY:
            prefix = code.split("-")[0]
            assert LOCATION_PREFIX_DISPLAY[prefix] == LOCATION_DISPLAY[code]


class TestLeasewebDescribeLocation:
    def test_ordering_only_location_has_metadata(self) -> None:
        from cloud_platform.providers.leaseweb.ordering import (
            LeaseWebOrderingProvider,
        )

        provider = LeaseWebOrderingProvider.__new__(LeaseWebOrderingProvider)
        described = LeaseWebOrderingProvider.describe_location(provider, "FRA-10")
        assert described.id == "FRA-10"
        assert described.country_code == "DE"
        assert described.city == "Frankfurt"


class TestHetznerCountryNormalization:
    def test_native_codes_pass_through(self) -> None:
        assert _normalize_country("DE") == "DE"
        assert _normalize_country("fi") == "FI"

    def test_ragged_payload_becomes_unknown(self) -> None:
        assert _normalize_country(None) is None
        assert _normalize_country("") is None
        assert _normalize_country("  ") is None
        assert _normalize_country("DEU") is None
        assert _normalize_country("D1") is None
        assert _normalize_country("xx-99") is None
