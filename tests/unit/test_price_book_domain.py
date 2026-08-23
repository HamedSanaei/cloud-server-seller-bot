"""Tests for the pricing domain: margin rules, versions, derivation (M06-001)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from cloud_platform.modules.pricing.domain import (
    MarginRule,
    NoMarginRuleError,
    OfferCost,
    PriceBookVersion,
    active_version,
    derive_selling_price,
)

T0 = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _rule(
    provider: str = "*",
    plan: str = "*",
    location: str = "*",
    factor: str = "1.0",
    fixed: int = 0,
) -> MarginRule:
    return MarginRule(
        provider=provider,
        plan=plan,
        location=location,
        margin_factor=Decimal(factor),
        fixed_minor=fixed,
    )


def _offer(**overrides: object) -> OfferCost:
    defaults: dict[str, object] = {
        "provider_key": "hetzner",
        "plan_id": "cx22",
        "location_id": "fsn1",
        "cost_minor": 100,
        "currency": "EUR",
    }
    defaults.update(overrides)
    return OfferCost(**defaults)  # type: ignore[arg-type]


def _version(
    rules: tuple[MarginRule, ...],
    *,
    book: str = "retail-eur",
    version: int = 1,
    effective_at: datetime = T0,
) -> PriceBookVersion:
    return PriceBookVersion(book_name=book, version=version, effective_at=effective_at, rules=rules)


class TestMarginRule:
    def test_wildcard_specificity_zero(self) -> None:
        assert _rule().specificity() == 0

    def test_specificity_counts_concrete_fields(self) -> None:
        assert _rule("hetzner").specificity() == 1
        assert _rule("hetzner", "cx22").specificity() == 2
        assert _rule("hetzner", "cx22", "fsn1").specificity() == 3

    def test_matches(self) -> None:
        offer = _offer()
        assert _rule().matches(offer)
        assert _rule("hetzner").matches(offer)
        assert _rule("hetzner", "cx22", "fsn1").matches(offer)
        assert not _rule("other").matches(offer)
        assert not _rule("hetzner", "cx32").matches(offer)
        assert not _rule("hetzner", "cx22", "nbg1").matches(offer)

    def test_empty_or_blank_pattern_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider"):
            _rule(provider="")
        with pytest.raises(ValueError, match="plan"):
            _rule(plan="   ")
        with pytest.raises(ValueError, match="location"):
            _rule(location="")

    def test_factor_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="margin_factor"):
            _rule(factor="0")
        with pytest.raises(ValueError, match="margin_factor"):
            _rule(factor="-1")

    def test_fixed_must_not_be_negative(self) -> None:
        with pytest.raises(ValueError, match="fixed_minor"):
            _rule(fixed=-1)


class TestOfferCost:
    @pytest.mark.parametrize("field", ["provider_key", "plan_id", "location_id", "currency"])
    def test_empty_fields_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            _offer(**{field: ""})

    def test_negative_cost_rejected(self) -> None:
        with pytest.raises(ValueError, match="cost_minor"):
            _offer(cost_minor=-1)


class TestPriceBookVersion:
    def test_valid_version(self) -> None:
        v = _version((_rule(factor="1.1"),))
        assert v.version == 1
        assert v.id is None

    def test_version_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="version"):
            _version((_rule(),), version=0)

    def test_empty_book_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="book_name"):
            _version((_rule(),), book="  ")

    def test_naive_effective_at_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            _version((_rule(),), effective_at=datetime(2026, 8, 23))

    def test_empty_rules_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one rule"):
            _version(())

    def test_duplicate_patterns_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate rule pattern"):
            _version((_rule("hetzner", "cx22"), _rule("hetzner", "cx22")))

    def test_distinct_same_specificity_patterns_allowed(self) -> None:
        v = _version((_rule("hetzner", "cx22"), _rule("hetzner", "cx32")))
        assert len(v.rules) == 2


class TestActiveVersion:
    def test_picks_latest_effective(self) -> None:
        v1 = _version((_rule(factor="1.1"),), version=1, effective_at=T0)
        v2 = _version((_rule(factor="1.2"),), version=2, effective_at=T1)
        assert active_version([v2, v1], T0) is v1  # T1 not yet effective
        assert active_version([v2, v1], T1) is v2
        assert active_version([v2, v1], T1) is v2  # stable

    def test_no_effective_version(self) -> None:
        v1 = _version((_rule(),), effective_at=T1)
        assert active_version([v1], T0) is None

    def test_empty_list(self) -> None:
        assert active_version([], T0) is None


class TestDerivation:
    def test_exact_multiplier(self) -> None:
        v = _version((_rule(factor="1.15"),))
        result = derive_selling_price(v, _offer(cost_minor=100), T0)
        assert result.selling_minor == 115
        assert result.rule.margin_factor == Decimal("1.15")
        assert result.book_name == "retail-eur"
        assert result.version == 1
        assert result.priced_at == T0
        assert result.offer.cost_minor == 100

    def test_half_up_rounding(self) -> None:
        # 20 * 1.025 = 20.5 -> 21 (half-up)
        v = _version((_rule(factor="1.025"),))
        result = derive_selling_price(v, _offer(cost_minor=20), T0)
        assert result.selling_minor == 21
        # 5 * 1.15 = 5.75 -> 6
        v2 = _version((_rule(factor="1.15"),))
        result = derive_selling_price(v2, _offer(cost_minor=5), T0)
        assert result.selling_minor == 6

    def test_fixed_offset(self) -> None:
        v = _version((_rule(factor="1.0", fixed=50),))
        result = derive_selling_price(v, _offer(cost_minor=100), T0)
        assert result.selling_minor == 150

    def test_most_specific_rule_wins(self) -> None:
        v = _version(
            (
                _rule(factor="1.5"),  # default
                _rule("hetzner", factor="1.2"),  # provider
                _rule("hetzner", "cx22", factor="1.1"),  # plan
                _rule("hetzner", "cx22", "fsn1", factor="1.05"),  # exact
            )
        )
        result = derive_selling_price(v, _offer(cost_minor=100), T0)
        assert result.rule.pattern == ("hetzner", "cx22", "fsn1")
        assert result.selling_minor == 105

    def test_provider_rule_beats_default(self) -> None:
        v = _version((_rule(factor="1.5"), _rule("hetzner", factor="1.2")))
        result = derive_selling_price(v, _offer(cost_minor=100), T0)
        assert result.selling_minor == 120

    def test_no_matching_rule_raises(self) -> None:
        v = _version((_rule("other-provider"),))
        with pytest.raises(NoMarginRuleError):
            derive_selling_price(v, _offer(), T0)

    def test_deterministic(self) -> None:
        v = _version((_rule(factor="1.07", fixed=3),))
        first = derive_selling_price(v, _offer(cost_minor=77), T0)
        second = derive_selling_price(v, _offer(cost_minor=77), T0)
        assert first == second

    def test_zero_cost_still_charges_fixed(self) -> None:
        v = _version((_rule(factor="1.1", fixed=10),))
        result = derive_selling_price(v, _offer(cost_minor=0), T0)
        assert result.selling_minor == 10
