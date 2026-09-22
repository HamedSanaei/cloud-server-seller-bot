"""Dynamic Leaseweb location eligibility tests.

The configured locations are DISCOVERY SEEDS, never an authorization
allowlist: every candidate passes its own live read-only probe, and only
the probe verdict decides what can be sold. No test here performs a
billable POST; discovery uses GET reads exclusively.
"""

from __future__ import annotations

from typing import Any

from cloud_platform.providers.errors import (
    ProviderRateLimited,
    ProviderUnavailable,
)
from cloud_platform.providers.leaseweb.errors import (
    LeasewebAuthenticationError,
    LeasewebErrorPayload,
    LeasewebForbiddenError,
)
from cloud_platform.providers.leaseweb.ordering import (
    KNOWN_VPS_DATACENTERS,
    LeaseWebOrderingProvider,
    LocationEligibility,
    LocationProbe,
    classify_location_error,
    extract_location_codes,
    merge_candidates,
)

SALES_ORG_403 = (
    "You can only use this resource in your Sales Organization locations (FRA-01, FRA-10, FRA-14)."
)

#: Deliberately fake test credential (never a real key; never leaves tests).
KEY = "LSW-ELIGIBILITY-TEST-KEY"


def _forbidden(message: str) -> LeasewebForbiddenError:
    return LeasewebForbiddenError(
        message,
        payload=LeasewebErrorPayload(http_status=403, error_message=message),
    )


def _provider(**overrides: Any) -> LeaseWebOrderingProvider:
    return LeaseWebOrderingProvider(api_key=KEY, **overrides)


class TestErrorClassification:
    def test_sales_organization_403_is_ineligible(self) -> None:
        eligibility, _ = classify_location_error(_forbidden(SALES_ORG_403))
        assert eligibility is LocationEligibility.INELIGIBLE_ACCOUNT

    def test_british_spelling_is_recognized(self) -> None:
        eligibility, _ = classify_location_error(_forbidden("not in your sales organisation scope"))
        assert eligibility is LocationEligibility.INELIGIBLE_ACCOUNT

    def test_other_403_is_still_account_denial(self) -> None:
        eligibility, note = classify_location_error(_forbidden("forbidden"))
        assert eligibility is LocationEligibility.INELIGIBLE_ACCOUNT
        assert "forbidden" in note

    def test_401_is_fatal(self) -> None:
        eligibility, _ = classify_location_error(LeasewebAuthenticationError("nope"))
        assert eligibility is LocationEligibility.FATAL_AUTHENTICATION

    def test_429_is_transient(self) -> None:
        eligibility, _ = classify_location_error(ProviderRateLimited("slow down"))
        assert eligibility is LocationEligibility.TRANSIENT_THROTTLED

    def test_5xx_and_transport_errors_are_transient(self) -> None:
        for exc in (
            ProviderUnavailable("boom"),
            TimeoutError("timed out"),
            ConnectionError("refused"),
            RuntimeError("unexpected"),
        ):
            eligibility, _ = classify_location_error(exc)
            assert eligibility is LocationEligibility.TRANSIENT_UNKNOWN


class TestLocationCodeExtraction:
    def test_extracts_codes_from_sales_org_message(self) -> None:
        assert extract_location_codes(SALES_ORG_403) == ("FRA-01", "FRA-10", "FRA-14")

    def test_empty_and_code_free_text(self) -> None:
        assert extract_location_codes("") == ()
        assert extract_location_codes("nothing here 123") == ()

    def test_deduplicates_and_sorts(self) -> None:
        assert extract_location_codes("SFO-12 and SFO-12 then AMS-01") == ("AMS-01", "SFO-12")


class TestCandidateMerging:
    def test_ordered_union_normalizes(self) -> None:
        assert merge_candidates([" fra-01 ", ""], ("FRA-01", "AMS-01")) == ("FRA-01", "AMS-01")

    def test_empty_sources_are_valid(self) -> None:
        assert merge_candidates((), []) == ()


class TestSeedsAreNotAuthority:
    def test_empty_configuration_is_accepted(self) -> None:
        assert _provider(locations=()).discovery_seeds == ()

    def test_seeds_are_returned_verbatim(self) -> None:
        assert _provider(locations=("FRA-01",)).discovery_seeds == ("FRA-01",)

    def test_known_datacenters_cover_observed_geography(self) -> None:
        for code in (
            "AMS-01",
            "FRA-01",
            "FRA-10",
            "FRA-14",
            "LON-01",
            "MTL-02",
            "SFO-12",
            "SIN-01",
            "SYD-12",
            "TYO-11",
            "WDC-02",
        ):
            assert code in KNOWN_VPS_DATACENTERS

    def test_unknown_code_is_retained_not_discarded(self) -> None:
        provider = _provider()
        described = provider.describe_location("NEW-99")
        assert described.id == "NEW-99"
        assert described.name == "NEW-99"


class _ScriptedTransport:
    """Fake transport: scripted per-location catalog outcomes, records POSTs."""

    def __init__(self, outcomes: dict[str, Any]) -> None:
        self._outcomes = outcomes
        self.posts: list[str] = []
        self.gets: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.throttle = None

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.calls.append((method, path))
        params = kwargs.get("params") or {}
        if method == "GET" and path == "/ordering/v1/products/vps":
            location = params.get("location")
            self.gets.append(str(location))
            outcome = self._outcomes.get(str(location), ([], 0))
            if isinstance(outcome, Exception):
                raise outcome
            items, total = outcome
            return {"vpss": items, "_metadata": {"totalCount": total}}
        if method != "GET":
            self.posts.append(f"{method} {path}")
        raise AssertionError(f"unexpected request {method} {path}")

    async def aclose(self) -> None:
        return None


def _product(pid: str = "VPS02_1") -> dict[str, Any]:
    return {
        "id": pid,
        "name": "VPS",
        "vCpu": "4",
        "vRam": "6",
        "nvmeStorage": "100 GB",
        "traffic": "30 TB",
        "price": {"currency": "EUR", "total": 3.59},
    }


def _provider_with(outcomes: dict[str, Any], **kw: Any) -> LeaseWebOrderingProvider:
    provider = _provider(locations=("FRA-01", "AMS-01"), **kw)
    transport = _ScriptedTransport(outcomes)
    provider._transport = transport  # type: ignore[attr-defined]
    return provider


class TestProbeBehavior:
    async def test_eligible_location_returns_products(self) -> None:
        provider = _provider_with({"FRA-01": ([_product(), _product("VPS02_2")], 2)})
        probe = await provider.probe_location("FRA-01")
        assert probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE
        assert [p.id for p in probe.products] == ["VPS02_1", "VPS02_2"]

    async def test_empty_eligible_location(self) -> None:
        provider = _provider_with({"FRA-10": ([], 0)})
        probe = await provider.probe_location("FRA-10")
        assert probe.eligibility is LocationEligibility.ELIGIBLE_EMPTY
        assert probe.products == ()

    async def test_ineligible_location_harvests_message_codes(self) -> None:
        provider = _provider_with({"AMS-01": _forbidden(SALES_ORG_403)})
        probe = await provider.probe_location("AMS-01")
        assert probe.eligibility is LocationEligibility.INELIGIBLE_ACCOUNT
        assert probe.products == ()
        assert probe.discovered_locations == ("FRA-01", "FRA-10", "FRA-14")

    async def test_transient_failure_preserves_nothing_and_raises_nothing(self) -> None:
        provider = _provider_with({"AMS-01": ProviderUnavailable("down")})
        probe = await provider.probe_location("AMS-01")
        assert probe.eligibility is LocationEligibility.TRANSIENT_UNKNOWN

    async def test_location_codes_are_normalized(self) -> None:
        provider = _provider_with({"FRA-01": ([_product()], 1)})
        probe = await provider.probe_location("  fra-01 ")
        assert probe.location == "FRA-01"
        assert probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE

    async def test_unscoped_request_sends_no_location(self) -> None:
        seen: dict[str, Any] = {}

        class _Transport(_ScriptedTransport):
            async def request(self, method: str, path: str, **kwargs: Any) -> Any:
                seen.update(kwargs.get("params") or {})
                return {"vpss": [], "_metadata": {"totalCount": 0}}

        provider = _provider()
        provider._transport = _Transport({})  # type: ignore[attr-defined]
        await provider.list_products_unscoped()
        assert "location" not in seen

    async def test_no_discovery_path_posts(self) -> None:
        provider = _provider_with({"FRA-01": ([_product()], 1)})
        await provider.list_products_unscoped()
        await provider.probe_location("FRA-01")
        await provider.list_locations()
        transport = provider._transport
        assert isinstance(transport, _ScriptedTransport)
        assert transport.posts == []
        assert transport.calls and all(method == "GET" for method, _ in transport.calls)

    async def test_probe_is_read_only_for_unknown_codes(self) -> None:
        provider = _provider_with({"NEW-99": ([_product()], 1)})
        probe = await provider.probe_location("NEW-99")
        assert probe.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE
        assert probe.products[0].location == "NEW-99"

    async def test_verify_credential_scopes_the_read_when_nothing_is_configured(self) -> None:
        """An unscoped probe is NOT a valid authentication test for this provider.

        Production evidence: with no location configured the old probe issued a
        location-LESS catalog read, which real Leaseweb can refuse (403) while
        every location-scoped read for the same key succeeds. That reported two
        perfectly working credentials as invalid. With no candidates, the probe
        therefore falls back to the built-in datacenter seeds — still read-only,
        still bounded — and judges the key on scoped reads.
        """
        seen: list[dict[str, Any]] = []

        class _Transport(_ScriptedTransport):
            async def request_raw(self, method: str, path: str, **kwargs: Any) -> Any:
                seen.append(dict(kwargs.get("params") or {}))

                class _Response:
                    is_success = True
                    status_code = 200
                    content = b"{}"

                    def json(self) -> Any:
                        return {}

                return _Response()

        provider = _provider(locations=())
        provider._transport = _Transport({})  # type: ignore[attr-defined]
        await provider.verify_credential("candidate")
        assert seen, "the credential probe must issue at least one read"
        assert all("location" in params for params in seen)
        # One success is conclusive, so the probe stops immediately.
        assert len(seen) == 1


class TestProbeResultShape:
    def test_probe_is_a_plain_value(self) -> None:
        probe = LocationProbe("FRA-01", LocationEligibility.ELIGIBLE_EMPTY, (), (), "x")
        assert probe.location == "FRA-01"
        assert probe.products == ()
        assert probe.discovered_locations == ()
