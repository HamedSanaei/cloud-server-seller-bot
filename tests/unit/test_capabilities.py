"""Tests for the provider capability matrix contract."""

from __future__ import annotations

import pytest

from cloud_platform.providers.base import (
    Capability,
    CloudProvider,
    supports,
    supports_all,
    supports_any,
)


class _FullProvider:
    """A provider that supports all capabilities."""

    key = "full"
    capabilities = frozenset(Capability)


class _ComputeOnlyProvider:
    """A provider that supports only compute and power."""

    key = "minimal"
    capabilities = frozenset({Capability.COMPUTE, Capability.POWER})


class _EmptyProvider:
    """A provider that supports no capabilities."""

    key = "empty"
    capabilities: frozenset[Capability] = frozenset()


@pytest.fixture
def full_provider() -> CloudProvider:
    return _FullProvider()  # type: ignore[return-value]


@pytest.fixture
def compute_provider() -> CloudProvider:
    return _ComputeOnlyProvider()  # type: ignore[return-value]


@pytest.fixture
def empty_provider() -> CloudProvider:
    return _EmptyProvider()  # type: ignore[return-value]


def test_supports_single_capability_present(full_provider: CloudProvider) -> None:
    assert supports(full_provider, Capability.COMPUTE) is True


def test_supports_single_capability_absent(compute_provider: CloudProvider) -> None:
    assert supports(compute_provider, Capability.REBUILD) is False


def test_supports_all_present(full_provider: CloudProvider) -> None:
    assert supports_all(full_provider, {Capability.COMPUTE, Capability.POWER}) is True


def test_supports_all_partially_present(compute_provider: CloudProvider) -> None:
    assert supports_all(compute_provider, {Capability.COMPUTE, Capability.SNAPSHOT}) is False


def test_supports_all_empty_provider(empty_provider: CloudProvider) -> None:
    assert supports_all(empty_provider, set()) is True
    assert supports_all(empty_provider, {Capability.COMPUTE}) is False


def test_supports_any_present(compute_provider: CloudProvider) -> None:
    assert supports_any(compute_provider, {Capability.COMPUTE, Capability.SNAPSHOT}) is True


def test_supports_any_absent(compute_provider: CloudProvider) -> None:
    assert supports_any(compute_provider, {Capability.SNAPSHOT, Capability.REBUILD}) is False


def test_supports_any_empty_capabilities(compute_provider: CloudProvider) -> None:
    assert supports_any(compute_provider, set()) is False


def test_capability_enum_values() -> None:
    assert Capability.COMPUTE == "compute"
    assert Capability.FIREWALL == "firewall"
    assert Capability.FLOATING_IP == "floating_ip"


def test_capability_from_string() -> None:
    assert Capability("compute") == Capability.COMPUTE
    assert Capability("snapshot") == Capability.SNAPSHOT
