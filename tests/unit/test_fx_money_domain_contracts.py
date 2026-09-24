"""Money/currency domain invariants: audited exponents, Decimal-only bounds.

These tests pin the platform's money contract:

* every sellable currency has an AUDITED minor-unit exponent (JPY/KRW are
  exponent 0 and are never divided by 100),
* an unknown currency fails closed instead of being assumed to have cents,
* amounts are bounded by signed int64 and are never floats,
* a resolved conversion must agree with its own exact rate,
* a financially binding snapshot must carry an expiry and its provenance.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from cloud_platform.modules.fx.domain import (
    FX_MATH_PRECISION,
    ConversionSnapshot,
    FxInvalidQuoteError,
    FxMarketQuote,
    FxPurpose,
    FxReferenceQuote,
    FxUnsupportedCurrencyError,
    ResolvedMoney,
    currency_exponent,
    major_to_minor,
    minor_to_major,
    normalize_currency,
    parse_rate,
)

NOW = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)


def _resolved(**overrides: object) -> ResolvedMoney:
    """EUR 10.00 at 1.10 -> USD 11.00, the canonical exact conversion."""
    values: dict[str, object] = {
        "source_amount_minor": 1000,
        "source_currency": "EUR",
        "target_amount_minor": 1100,
        "target_currency": "USD",
        "rate": Decimal("1.10"),
        "purpose": FxPurpose.CHARGE,
        "source": "frankfurter",
        "path": "reference EUR/USD",
        "observed_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return ResolvedMoney(**values)  # type: ignore[arg-type]


def _snapshot(**overrides: object) -> ConversionSnapshot:
    values: dict[str, object] = {
        "source_amount_minor": 1000,
        "source_currency": "EUR",
        "target_amount_minor": 1100,
        "target_currency": "USD",
        "rate": Decimal("1.10"),
        "purpose": FxPurpose.CHARGE,
        "source": "frankfurter",
        "path": "reference EUR/USD",
        "observed_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(overrides)
    return ConversionSnapshot(**values)  # type: ignore[arg-type]


class TestAuditedCurrencyExponents:
    @pytest.mark.parametrize(
        "currency",
        ["USD", "EUR", "GBP", "SGD", "AUD", "CAD"],
    )
    def test_two_decimal_currencies(self, currency: str) -> None:
        assert currency_exponent(currency) == 2

    @pytest.mark.parametrize("currency", ["JPY", "KRW"])
    def test_zero_decimal_currencies(self, currency: str) -> None:
        assert currency_exponent(currency) == 0

    def test_unknown_currency_is_never_assumed_to_have_cents(self) -> None:
        with pytest.raises(FxUnsupportedCurrencyError, match="not audited"):
            currency_exponent("XYZ")

        with pytest.raises(FxUnsupportedCurrencyError):
            minor_to_major(100, "XYZ")

        with pytest.raises(FxUnsupportedCurrencyError):
            major_to_minor(Decimal("1.00"), "XYZ", FxPurpose.DISPLAY)

    def test_jpy_minor_units_are_yen_not_cents(self) -> None:
        assert minor_to_major(691, "JPY") == Decimal(691)
        assert major_to_minor(Decimal("691"), "JPY", FxPurpose.CHARGE) == 691
        # A sub-yen major amount rounds at the YEN boundary, not the cent one.
        assert major_to_minor(Decimal("6.91"), "JPY", FxPurpose.DISPLAY) == 7

    def test_eur_amounts_use_two_decimals(self) -> None:
        assert minor_to_major(809, "EUR") == Decimal("8.09")
        assert major_to_minor(Decimal("8.09"), "EUR", FxPurpose.CHARGE) == 809


class TestNormalizeCurrencyContract:
    @pytest.mark.parametrize("value", [None, 123, b"USD", ["USD"]])
    def test_non_string_is_refused(self, value: object) -> None:
        with pytest.raises(FxUnsupportedCurrencyError):
            normalize_currency(value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", ["", "U", "ABCDEF", "US1", "US D", "USD!"])
    def test_malformed_codes_are_refused(self, value: str) -> None:
        with pytest.raises(FxUnsupportedCurrencyError):
            normalize_currency(value)

    @pytest.mark.parametrize(
        ("raw", "expected"), [("usd", "USD"), (" eur ", "EUR"), ("usdt", "USDT")]
    )
    def test_codes_are_canonicalized(self, raw: str, expected: str) -> None:
        assert normalize_currency(raw) == expected


class TestAmountBounds:
    @pytest.mark.parametrize("amount", [True, 1.5, "100", 2**63])
    def test_minor_to_major_rejects_non_int64_integers(self, amount: object) -> None:
        with pytest.raises(ValueError, match="signed 64-bit"):
            minor_to_major(amount, "USD")  # type: ignore[arg-type]

    @pytest.mark.parametrize("amount", ["1.00", 1.0, 1])
    def test_major_to_minor_requires_a_decimal(self, amount: object) -> None:
        with pytest.raises(ValueError, match="must be a Decimal"):
            major_to_minor(amount, "USD", FxPurpose.CHARGE)  # type: ignore[arg-type]

    def test_major_to_minor_rejects_amounts_outside_int64(self) -> None:
        with pytest.raises(ValueError, match="signed 64-bit"):
            major_to_minor(Decimal("1E30"), "USD", FxPurpose.CHARGE)

    @pytest.mark.parametrize(
        ("amount", "purpose", "expected"),
        [
            # 1.001 USD: display rounds half-up DOWN to 1.00, a charge rounds UP.
            ("1.001", FxPurpose.DISPLAY, 100),
            ("1.001", FxPurpose.CHARGE, 101),
            ("1.001", FxPurpose.LIQUIDATION, 100),
            # 1.019 USD: display rounds half-up UP, liquidation still floors.
            ("1.019", FxPurpose.DISPLAY, 102),
            ("1.019", FxPurpose.CHARGE, 102),
            ("1.019", FxPurpose.LIQUIDATION, 101),
        ],
    )
    def test_rounding_is_purpose_directed(
        self, amount: str, purpose: FxPurpose, expected: int
    ) -> None:
        assert major_to_minor(Decimal(amount), "USD", purpose) == expected


class TestParseRateContract:
    @pytest.mark.parametrize("raw", [True, False, 1.145])
    def test_float_and_boolean_rates_are_refused(self, raw: object) -> None:
        with pytest.raises(FxInvalidQuoteError, match="malformed rate field"):
            parse_rate(raw, field="rate")

    @pytest.mark.parametrize("raw", ["", "abc", None])
    def test_unparsable_rates_are_refused(self, raw: object) -> None:
        with pytest.raises(FxInvalidQuoteError, match="malformed rate field"):
            parse_rate(raw, field="rate")

    @pytest.mark.parametrize("raw", ["0", "-1", "NaN", "Infinity"])
    def test_non_positive_or_non_finite_rates_are_refused(self, raw: str) -> None:
        with pytest.raises(FxInvalidQuoteError, match="non-positive or non-finite"):
            parse_rate(raw, field="rate")

    def test_exact_decimal_text_is_preserved(self) -> None:
        assert parse_rate("1.145", field="rate") == Decimal("1.145")


class TestQuoteTimestampContract:
    @pytest.mark.parametrize(
        ("observed", "expires", "match"),
        [
            (datetime(2026, 3, 4, 12, 0), NOW + timedelta(hours=1), "timezone-aware"),
            (NOW, datetime(2026, 3, 4, 13, 0), "timezone-aware"),
            (NOW, NOW, "must be after observed_at"),
        ],
    )
    def test_timestamps_that_cannot_express_freshness_are_refused(
        self, observed: object, expires: object, match: str
    ) -> None:
        with pytest.raises(FxInvalidQuoteError, match=match):
            FxReferenceQuote(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.10"),
                source="frankfurter",
                source_market="EUR/USD",
                provider_date=datetime.now(UTC).date(),
                observed_at=observed,  # type: ignore[arg-type]
                expires_at=expires,  # type: ignore[arg-type]
            )

    def test_future_observation_is_refused(self) -> None:
        live = datetime.now(UTC)

        with pytest.raises(FxInvalidQuoteError, match="must not be in the future"):
            FxReferenceQuote(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.10"),
                source="frankfurter",
                source_market="EUR/USD",
                provider_date=live.date(),
                observed_at=live + timedelta(hours=1),
                expires_at=live + timedelta(hours=2),
            )

    def test_non_datetime_timestamps_are_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="must be datetime values"):
            FxReferenceQuote(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.10"),
                source="frankfurter",
                source_market="EUR/USD",
                provider_date=datetime.now(UTC).date(),
                observed_at="2026-03-04T12:00:00+00:00",  # type: ignore[arg-type]
                expires_at=NOW + timedelta(hours=1),
            )


class TestReferenceQuoteContract:
    @staticmethod
    def _quote(**overrides: object) -> FxReferenceQuote:
        values: dict[str, object] = {
            "base_currency": "EUR",
            "quote_currency": "USD",
            "rate": Decimal("1.145"),
            "source": "frankfurter",
            "source_market": "EUR/USD",
            "provider_date": datetime.now(UTC).date(),
            "observed_at": NOW,
            "expires_at": NOW + timedelta(hours=1),
        }
        values.update(overrides)
        return FxReferenceQuote(**values)  # type: ignore[arg-type]

    def test_float_rate_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="must be a Decimal"):
            self._quote(rate=1.145)

    def test_datetime_provider_date_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="must be a date value"):
            self._quote(provider_date=NOW)

    def test_future_provider_date_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="must not be in the future"):
            self._quote(provider_date=datetime.now(UTC).date() + timedelta(days=1))

    @pytest.mark.parametrize("field", ["source", "source_market"])
    def test_blank_provenance_is_refused(self, field: str) -> None:
        with pytest.raises(FxInvalidQuoteError, match="must not be empty"):
            self._quote(**{field: "   "})


class TestMarketQuoteContract:
    @staticmethod
    def _quote(**overrides: object) -> FxMarketQuote:
        values: dict[str, object] = {
            "base_currency": "EUR",
            "quote_currency": "IRT",
            "buy_rate": Decimal("1500000"),
            "sell_rate": Decimal("1400000"),
            "source": "abantether",
            "source_market": "EURIRT",
            "observed_at": NOW,
            "expires_at": NOW + timedelta(minutes=1),
        }
        values.update(overrides)
        return FxMarketQuote(**values)  # type: ignore[arg-type]

    def test_float_buy_rate_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="buy_rate"):
            self._quote(buy_rate=1500000.0)

    def test_non_finite_sell_rate_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="sell_rate"):
            self._quote(sell_rate=Decimal("-1"))

    @pytest.mark.parametrize("field", ["source", "source_market"])
    def test_blank_provenance_is_refused(self, field: str) -> None:
        with pytest.raises(FxInvalidQuoteError, match="must not be empty"):
            self._quote(**{field: ""})

    def test_non_boolean_proxy_flag_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="proxy must be a boolean"):
            self._quote(proxy="yes")


class TestResolvedMoneyContract:
    def test_valid_conversion_is_accepted(self) -> None:
        resolved = _resolved()

        assert resolved.source_currency == "EUR"
        assert resolved.target_currency == "USD"
        assert resolved.target_amount_minor == 1100

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("source_amount_minor", True),
            ("source_amount_minor", -1),
            ("source_amount_minor", 2**63),
            ("target_amount_minor", 1.5),
            ("target_amount_minor", -1),
        ],
    )
    def test_minor_amounts_must_be_non_negative_int64(self, field: str, value: object) -> None:
        with pytest.raises(ValueError, match="must be a non-negative int"):
            _resolved(**{field: value})

    @pytest.mark.parametrize("rate", [1.10, 0, Decimal("-1"), Decimal("NaN")])
    def test_rate_must_be_a_positive_finite_decimal(self, rate: object) -> None:
        with pytest.raises(ValueError, match="rate must be a positive finite Decimal"):
            _resolved(rate=rate)

    @pytest.mark.parametrize("rate", ["1E-1001", "1." + "1" * (FX_MATH_PRECISION + 5)])
    def test_rate_must_stay_inside_the_audited_numeric_range(self, rate: str) -> None:
        with pytest.raises(ValueError, match="audited numeric range"):
            _resolved(rate=Decimal(rate))

    def test_purpose_must_be_an_fx_purpose(self) -> None:
        with pytest.raises(ValueError, match="purpose must be an FxPurpose"):
            _resolved(purpose="charge")

    @pytest.mark.parametrize("field", ["source", "path"])
    def test_provenance_must_be_non_empty(self, field: str) -> None:
        with pytest.raises(ValueError, match="provenance must be non-empty"):
            _resolved(**{field: "  "})

    def test_amounts_must_agree_with_the_exact_rate(self) -> None:
        with pytest.raises(ValueError, match="do not match the exact rate"):
            _resolved(target_amount_minor=1099)

    def test_proxy_conversion_must_identify_usdt(self) -> None:
        with pytest.raises(ValueError, match="must identify the audited USDT asset"):
            _resolved(proxy=True, proxy_asset="")

        with pytest.raises(ValueError, match="must not identify a proxy asset"):
            _resolved(proxy=False, proxy_asset="USDT")

    def test_non_boolean_stale_flag_is_refused(self) -> None:
        with pytest.raises(ValueError, match="stale must be boolean"):
            _resolved(stale="yes")


class TestConversionSnapshotContract:
    def test_valid_snapshot_round_trips_through_its_dict(self) -> None:
        snapshot = _snapshot()

        restored = ConversionSnapshot.from_dict(dict(snapshot.to_dict()))

        assert restored == snapshot
        assert restored.target_amount_minor == 1100
        assert restored.purpose is FxPurpose.CHARGE

    def test_from_resolved_freezes_the_conversion(self) -> None:
        frozen = ConversionSnapshot.from_resolved(_resolved())

        assert frozen.source_amount_minor == 1000
        assert frozen.target_amount_minor == 1100
        assert frozen.rate == Decimal("1.10")
        assert frozen.expires_at is not None

    @pytest.mark.parametrize(
        ("field", "value"),
        [("source_amount_minor", -1), ("target_amount_minor", True), ("rate", 1.10)],
    )
    def test_invalid_amounts_or_rate_are_refused(self, field: str, value: object) -> None:
        with pytest.raises(FxInvalidQuoteError):
            _snapshot(**{field: value})

    def test_purpose_must_be_an_fx_purpose(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="purpose is invalid"):
            _snapshot(purpose="charge")

    def test_amounts_must_agree_with_the_exact_rate(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="do not match the exact rate"):
            _snapshot(target_amount_minor=1200)

    def test_observed_at_must_be_timezone_aware(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="timezone-aware"):
            _snapshot(observed_at=datetime(2026, 3, 4, 12, 0))

    def test_expiry_must_be_later_than_observation(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="later aware datetime"):
            _snapshot(expires_at=NOW)

    @pytest.mark.parametrize("purpose", [FxPurpose.CHARGE, FxPurpose.LIQUIDATION])
    def test_binding_snapshots_require_an_expiry(self, purpose: FxPurpose) -> None:
        with pytest.raises(FxInvalidQuoteError, match="require an expiry timestamp"):
            _snapshot(purpose=purpose, expires_at=None)

    def test_float_rate_for_a_binding_snapshot_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="positive finite Decimal"):
            _snapshot(rate=1.1)

    def test_non_boolean_stale_flag_is_refused(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="stale flag must be boolean"):
            _snapshot(stale="yes")

    @pytest.mark.parametrize("field", ["source", "path"])
    def test_blank_provenance_is_refused(self, field: str) -> None:
        with pytest.raises(FxInvalidQuoteError, match="provenance must be non-empty"):
            _snapshot(**{field: ""})

    def test_proxy_flags_must_be_consistent(self) -> None:
        with pytest.raises(FxInvalidQuoteError, match="boolean"):
            _snapshot(proxy="yes")

        with pytest.raises(FxInvalidQuoteError, match="must identify the audited USDT asset"):
            _snapshot(proxy=True, proxy_asset="BTC")

        with pytest.raises(FxInvalidQuoteError, match="must not identify a proxy asset"):
            _snapshot(proxy=False, proxy_asset="USDT")

    def test_from_dict_rejects_non_boolean_proxy_and_stale_text(self) -> None:
        payload = dict(_snapshot().to_dict())

        with pytest.raises(FxInvalidQuoteError, match="proxy must be true or false"):
            ConversionSnapshot.from_dict({**payload, "proxy": "maybe"})

        with pytest.raises(FxInvalidQuoteError, match="stale must be true or false"):
            ConversionSnapshot.from_dict({**payload, "stale": "2"})

    def test_from_dict_rejects_a_boolean_amount(self) -> None:
        payload = dict(_snapshot().to_dict())

        with pytest.raises(FxInvalidQuoteError, match="cannot be boolean"):
            ConversionSnapshot.from_dict({**payload, "source_amount_minor": True})
