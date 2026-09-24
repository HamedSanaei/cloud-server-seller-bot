"""Image compatibility corners: envelopes, empty-vs-missing, arch, fail-closed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from cloud_platform.modules.catalog.image_compatibility import (
    image_compatible,
    image_restriction_values,
)


def _image(metadata: Any = None, architecture: str = "") -> SimpleNamespace:
    return SimpleNamespace(metadata={} if metadata is None else metadata, architecture=architecture)


def test_missing_restriction_key_is_unrestricted() -> None:
    assert image_compatible(_image({}), plan_id="p1") is True
    assert image_compatible(_image({}), location_id="eu-west", account_id="a1") is True


def test_unknown_adapter_keys_are_unrestricted() -> None:
    image = _image({"display_name": "Ubuntu 22.04", "size_gb": 10})
    assert image_compatible(image, plan_id="p1", location_id="eu-west") is True


def test_present_empty_list_is_incompatible() -> None:
    assert image_compatible(_image({"plan_ids": []}), plan_id="p1") is False


def test_present_none_is_incompatible() -> None:
    assert image_compatible(_image({"plan_ids": None}), plan_id="p1") is False


def test_restrictions_envelope_match_and_mismatch() -> None:
    image = _image({"restrictions": {"plan_ids": ["p1"]}})
    assert image_compatible(image, plan_id="p1") is True
    assert image_compatible(image, plan_id="p2") is False


def test_metadata_envelope_for_locations() -> None:
    image = _image({"metadata": {"locations": ["eu-west"]}})
    assert image_compatible(image, location_id="eu-west") is True
    assert image_compatible(image, location_id="us-east") is False


def test_non_dict_envelope_fails_closed() -> None:
    image = _image({"restrictions": ["p1"]})
    assert image_compatible(image, plan_id="p1") is False


def test_non_dict_metadata_fails_closed() -> None:
    image = _image(["p1"])
    assert image_compatible(image, plan_id="p1") is False
    assert image_restriction_values(image, ("plan_ids",)) is None


def test_architecture_exact_match_is_case_insensitive() -> None:
    assert image_compatible(_image({}, "X86_64"), architecture="x86_64") is True
    assert image_compatible(_image({}, "arm64"), architecture="x86_64") is False


def test_architecture_from_metadata_dict() -> None:
    image = _image({"architecture": "x86_64"})
    assert image_compatible(image, architecture="x86_64") is True
    assert image_compatible(image, architecture="arm64") is False


def test_location_and_account_must_both_match() -> None:
    image = _image({"location_ids": ["eu-west"], "account_ids": ["a1"]})
    assert image_compatible(image, location_id="eu-west", account_id="a1") is True
    assert image_compatible(image, location_id="us-east", account_id="a1") is False
    assert image_compatible(image, location_id="eu-west", account_id="a2") is False


def test_dict_valued_restriction_uses_values() -> None:
    image = _image({"plan_ids": {"slot-a": "p1"}})
    assert image_compatible(image, plan_id="p1") is True
    assert image_compatible(image, plan_id="slot-a") is False


def test_restriction_values_absent_vs_empty() -> None:
    assert image_restriction_values(_image({}), ("plan_ids",)) is None
    assert image_restriction_values(_image({"plan_ids": []}), ("plan_ids",)) == set()
    assert image_restriction_values(_image({"plan_ids": ["p1"]}), ("plan_ids",)) == {"p1"}
