"""Offline tests for the price-book publish ops script (no DB)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts import publish_price_book as ppb


def test_parse_rules_file_validates_and_builds_decimal_rules(tmp_path) -> None:
    rules_file = tmp_path / "margins.json"
    rules_file.write_text(
        '[{"provider": "hetzner", "plan": "*", "location": "*",'
        ' "margin_factor": "1.20", "fixed_minor": 50},'
        ' {"provider": "leaseweb", "plan": "*", "location": "AMS-01",'
        ' "margin_factor": "1.25", "monthly_cap_minor": 500000}]',
        encoding="utf-8",
    )
    rules = ppb._parse_rules_file(str(rules_file))
    assert len(rules) == 2
    assert rules[0].margin_factor == Decimal("1.20")
    assert rules[0].fixed_minor == 50
    assert rules[1].monthly_cap_minor == 500000
    # Money is Decimal-only: no float survives parsing.
    assert not isinstance(rules[0].margin_factor, float)


def test_parse_rules_file_rejects_empty_list(tmp_path) -> None:
    rules_file = tmp_path / "margins.json"
    rules_file.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        ppb._parse_rules_file(str(rules_file))


def test_parse_rules_file_rejects_bad_margin(tmp_path) -> None:
    rules_file = tmp_path / "margins.json"
    rules_file.write_text(
        '[{"provider": "*", "plan": "*", "location": "*", "margin_factor": "0"}]',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="margin_factor"):
        ppb._parse_rules_file(str(rules_file))


def test_main_requires_reason_and_admin() -> None:
    with pytest.raises(SystemExit):
        ppb.main([])
