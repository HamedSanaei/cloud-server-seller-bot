"""The accepted hourly contract must be complete and self-consistent.

``_validate_hourly_contract`` is what turns a stored ``offer_fingerprint``
plus its immutable ``ServerPriceSnapshot`` into a trustworthy billable
contract. If any part of it is missing, mis-typed, or disagrees with the
stored money columns, the server must be refused instead of billed from a
reconstructed or rounded price.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.hourly.service import (
    HourlyNotAvailableError,
    _provider_hourly_rate,
    _validate_hourly_contract,
)
from cloud_platform.modules.offers.domain import (
    BILLING_MODEL_HOURLY,
    BILLING_MODEL_PREPAID_MONTHLY,
)

PROVIDER_KEY = "leaseweb"
PRODUCT_ID = "lsw.mini"
LOCATION_ID = "eu-west-3"
IMAGE_ID = "UBUNTU-24-04"
ACCOUNT_ID = "acct-1"
# Exact provider rate whose DISPLAY minor projection is provider_cost_minor=5.
EXACT_RATE = "0.0453"
COST_MINOR = 5
SELLING_MINOR = 6


def _fingerprint(**overrides: object) -> dict[str, Any]:
    fingerprint: dict[str, Any] = {
        "fingerprint_version": 1,
        "offer_id": str(uuid4()),
        "provider_key": PROVIDER_KEY,
        "product_id": PRODUCT_ID,
        "location_id": LOCATION_ID,
        "billing_model": BILLING_MODEL_HOURLY,
        "selling_currency": "USD",
        "provider_cost_currency": "EUR",
        "provider_rate_exact": EXACT_RATE,
        "image_id": IMAGE_ID,
        "selling_price_minor": SELLING_MINOR,
        "provider_cost_minor": COST_MINOR,
        "billing_parameters": {"provider_hourly_rate": EXACT_RATE},
        "pricing_metadata": {"pricing_schema_version": 1, "source_currency": "EUR"},
        "technical_metadata": {},
        "provider_account_id": ACCOUNT_ID,
        "credential_account_id": ACCOUNT_ID,
    }
    fingerprint.update(overrides)
    return fingerprint


def _contract(**overrides: object) -> tuple[SimpleNamespace, SimpleNamespace]:
    """A complete, self-consistent accepted hourly contract."""
    fingerprint = _fingerprint(**overrides)
    server = SimpleNamespace(
        provider_key=PROVIDER_KEY,
        billing_model=BILLING_MODEL_HOURLY,
        credential_account_id=ACCOUNT_ID,
        image_id=IMAGE_ID,
        offer_fingerprint=fingerprint,
    )
    raw_metadata = fingerprint["pricing_metadata"]
    snapshot = SimpleNamespace(
        offer_fingerprint=fingerprint,
        selling_minor=fingerprint["selling_price_minor"],
        selling_currency=fingerprint["selling_currency"],
        pricing_metadata=(dict(raw_metadata) if isinstance(raw_metadata, dict) else raw_metadata),
        offer=SimpleNamespace(
            provider_key=fingerprint["provider_key"],
            plan_id=fingerprint["product_id"],
            location_id=fingerprint["location_id"],
            cost_minor=fingerprint["provider_cost_minor"],
            currency=fingerprint["provider_cost_currency"],
            provider_rate_exact=fingerprint["provider_rate_exact"],
        ),
    )
    return server, snapshot


class TestConsistentContractIsAccepted:
    def test_complete_contract_has_no_complaint(self) -> None:
        server, snapshot = _contract()

        _validate_hourly_contract(server, snapshot)

    def test_exact_hourly_rate_is_preserved_verbatim(self) -> None:
        _server, snapshot = _contract()

        assert snapshot.offer.provider_rate_exact == "0.0453"
        assert (
            _provider_hourly_rate(
                SimpleNamespace(billing_parameters={"provider_hourly_rate": EXACT_RATE})
            )
            == "0.0453"
        )


class TestFingerprintShapeViolations:
    @pytest.mark.parametrize(
        ("overrides", "match"),
        [
            ({"product_id": "   "}, "field 'product_id' is invalid"),
            ({"provider_key": 123}, "field 'provider_key' is invalid"),
            ({"offer_id": "not-a-uuid"}, "offer_id is invalid"),
            ({"selling_price_minor": 0}, "field 'selling_price_minor' is invalid"),
            ({"selling_price_minor": True}, "field 'selling_price_minor' is invalid"),
            ({"provider_cost_minor": -1}, "field 'provider_cost_minor' is invalid"),
            ({"billing_parameters": []}, "field 'billing_parameters' is not a mapping"),
            ({"pricing_metadata": "nope"}, "field 'pricing_metadata' is not a mapping"),
            ({"technical_metadata": None}, "field 'technical_metadata' is not a mapping"),
            ({"provider_rate_exact": "abc"}, "exact rate is invalid"),
            ({"provider_rate_exact": "9.99"}, "exact rate/cost mismatch"),
            ({"billing_model": BILLING_MODEL_PREPAID_MONTHLY}, "non-hourly billing model"),
        ],
    )
    def test_fingerprint_field_violations_fail_closed(
        self, overrides: dict[str, object], match: str
    ) -> None:
        server, snapshot = _contract(**overrides)

        with pytest.raises(HourlyNotAvailableError, match=match):
            _validate_hourly_contract(server, snapshot)

    @pytest.mark.parametrize("fingerprint", [None, "fingerprint", 42, {}])
    def test_unversioned_fingerprint_fails_closed(self, fingerprint: object) -> None:
        server, snapshot = _contract()
        server.offer_fingerprint = fingerprint

        with pytest.raises(HourlyNotAvailableError, match="no versioned offer fingerprint"):
            _validate_hourly_contract(server, snapshot)

    def test_wrong_fingerprint_version_fails_closed(self) -> None:
        server, snapshot = _contract(fingerprint_version=2)

        with pytest.raises(HourlyNotAvailableError, match="no versioned offer fingerprint"):
            _validate_hourly_contract(server, snapshot)

    def test_snapshot_fingerprint_must_match_the_server(self) -> None:
        server, snapshot = _contract()
        snapshot.offer_fingerprint = _fingerprint()

        with pytest.raises(HourlyNotAvailableError, match="does not match the server"):
            _validate_hourly_contract(server, snapshot)


class TestServerAndSnapshotAgreement:
    """A frozen contract is all-or-nothing across both stored rows."""

    def test_provider_key_must_match(self) -> None:
        server, snapshot = _contract()
        server.provider_key = "hetzner"

        with pytest.raises(HourlyNotAvailableError, match="provider differs"):
            _validate_hourly_contract(server, snapshot)

    def test_billing_model_must_match(self) -> None:
        server, snapshot = _contract()
        server.billing_model = BILLING_MODEL_PREPAID_MONTHLY

        with pytest.raises(HourlyNotAvailableError, match="billing model differs"):
            _validate_hourly_contract(server, snapshot)

    def test_credential_account_must_match(self) -> None:
        server, snapshot = _contract()
        server.credential_account_id = "acct-2"

        with pytest.raises(HourlyNotAvailableError, match="credential account is inconsistent"):
            _validate_hourly_contract(server, snapshot)

    def test_image_identity_must_match(self) -> None:
        server, snapshot = _contract()
        server.image_id = "DEBIAN-12"

        with pytest.raises(HourlyNotAvailableError, match="image differs"):
            _validate_hourly_contract(server, snapshot)

    def test_pricing_metadata_must_match(self) -> None:
        server, snapshot = _contract()
        snapshot.pricing_metadata = {}

        with pytest.raises(HourlyNotAvailableError, match="pricing metadata disagrees"):
            _validate_hourly_contract(server, snapshot)

    @pytest.mark.parametrize(
        ("attribute", "value", "match"),
        [
            ("cost_minor", 6, "field 'provider_cost_minor' is inconsistent"),
            ("currency", "USD", "field 'provider_cost_currency' is inconsistent"),
            ("plan_id", "other.plan", "field 'product_id' is inconsistent"),
            ("location_id", "us-east-1", "field 'location_id' is inconsistent"),
            ("provider_rate_exact", "0.0500", "field 'provider_rate_exact' is inconsistent"),
        ],
    )
    def test_snapshot_offer_must_agree_with_the_fingerprint(
        self, attribute: str, value: object, match: str
    ) -> None:
        server, snapshot = _contract()
        setattr(snapshot.offer, attribute, value)

        with pytest.raises(HourlyNotAvailableError, match=match):
            _validate_hourly_contract(server, snapshot)

    @pytest.mark.parametrize(
        ("selling_minor", "currency", "match"),
        [
            (SELLING_MINOR + 1, "USD", "field 'selling_price_minor' is inconsistent"),
            (SELLING_MINOR, "EUR", "field 'selling_currency' is inconsistent"),
        ],
    )
    def test_snapshot_selling_price_must_agree_with_the_fingerprint(
        self, selling_minor: int, currency: str, match: str
    ) -> None:
        server, snapshot = _contract()
        snapshot.selling_minor = selling_minor
        snapshot.selling_currency = currency

        with pytest.raises(HourlyNotAvailableError, match=match):
            _validate_hourly_contract(server, snapshot)


class TestExactProviderHourlyRateAccessor:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("0.0453", "0.0453"), (" 0.0453 ", "0.0453"), (Decimal("0.0453"), "0.0453")],
    )
    def test_exact_rate_text_is_returned_verbatim(self, raw: object, expected: str) -> None:
        offer = SimpleNamespace(billing_parameters={"provider_hourly_rate": raw})

        assert _provider_hourly_rate(offer) == expected

    @pytest.mark.parametrize(
        "parameters",
        [None, {}, {"provider_hourly_rate": None}, {"provider_hourly_rate": 0.0453}],
    )
    def test_missing_or_non_decimal_rate_fails_closed(self, parameters: object) -> None:
        offer = SimpleNamespace(billing_parameters=parameters)

        with pytest.raises(HourlyNotAvailableError, match="missing exact provider_hourly_rate"):
            _provider_hourly_rate(offer)

    def test_blank_rate_text_fails_closed(self) -> None:
        offer = SimpleNamespace(billing_parameters={"provider_hourly_rate": "   "})

        with pytest.raises(HourlyNotAvailableError, match="missing exact provider_hourly_rate"):
            _provider_hourly_rate(offer)
