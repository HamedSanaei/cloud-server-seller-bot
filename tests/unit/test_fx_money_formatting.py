"""Money presentation: one formatter, audited exponents, no phantom cents.

JPY/KRW must render their exponent-0 amounts verbatim (¥691, never ¥6.91), and
an unaudited currency must never silently be rendered as if it had cents unless
an explicitly-legacy operator caller opts in.
"""

from __future__ import annotations

import pytest

from cloud_platform.modules.fx.formatting import format_minor, format_minor_signed


class TestFormatterInputContract:
    @pytest.mark.parametrize("amount", [True, False, 12.5, "1200", None])
    def test_non_integer_amounts_are_refused(self, amount: object) -> None:
        with pytest.raises(ValueError, match="never float"):
            format_minor(amount, "USD")  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="never float"):
            format_minor_signed(amount, "USD")  # type: ignore[arg-type]


class TestDomesticPresentation:
    def test_irt_uses_toman_units_verbatim(self) -> None:
        assert format_minor(1234567, "IRT") == "1,234,567 تومان"
        assert format_minor_signed(1234567, "IRT") == "+1,234,567 تومان"
        assert format_minor_signed(-1234567, "IRT") == "-1,234,567 تومان"

    def test_irr_uses_rial_units_verbatim(self) -> None:
        assert format_minor(98765, "IRR") == "98,765 ریال"
        assert format_minor_signed(-98765, "IRR") == "-98,765 ریال"


class TestGlobalPresentation:
    def test_usd_and_eur_use_their_storefront_symbols(self) -> None:
        assert format_minor(12345, "USD") == "$123.45"
        assert format_minor(12345, "EUR") == "€123.45"
        assert format_minor(-12345, "USD") == "-$123.45"

    def test_other_audited_currencies_keep_an_explicit_code(self) -> None:
        assert format_minor(12345, "GBP") == "123.45 GBP"
        assert format_minor(12345, "SGD") == "123.45 SGD"

    @pytest.mark.parametrize(("currency", "expected"), [("JPY", "¥691"), ("KRW", "₩1,300")])
    def test_zero_exponent_currencies_are_never_divided_by_100(
        self, currency: str, expected: str
    ) -> None:
        assert format_minor(691 if currency == "JPY" else 1300, currency) == expected

    def test_signed_global_amounts_carry_the_ledger_sign(self) -> None:
        assert format_minor_signed(12345, "USD") == "+$123.45"
        assert format_minor_signed(-12345, "USD") == "-$123.45"
        assert format_minor_signed(691, "JPY") == "+¥691"

    def test_empty_currency_uses_the_anonymous_two_decimal_legacy_shape(self) -> None:
        assert format_minor(12345, "") == "123.45"
        assert format_minor(-12345, "") == "-123.45"

    def test_unaudited_currency_requires_an_explicit_legacy_opt_in(self) -> None:
        with pytest.raises(ValueError, match="not supported for money display"):
            format_minor(12345, "XYZ")

        assert format_minor(12345, "XYZ", allow_legacy=True) == "123.45 XYZ"
