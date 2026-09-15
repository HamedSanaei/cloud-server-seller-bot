"""Tests for the provider registry's credential-account routing surface.

The registry is where "one logical provider key, many credential accounts"
becomes concrete. These tests pin the discipline that makes the split safe:

- a pinned resource resolves to ITS OWN account adapter;
- an account that is no longer configured fails closed;
- the reserved legacy ``default`` alias keeps pre-migration rows routable;
- the logical lookup (used by catalog/storefront code) never moves.
"""

from __future__ import annotations

import logging

import pytest

from cloud_platform.providers.registry import ProviderRegistry
from cloud_platform.providers.routing import (
    DEFAULT_CREDENTIAL_ACCOUNT,
    CredentialAccountState,
    CredentialAccountView,
    UnknownCredentialAccountError,
)


class DummyProvider:
    key = "dummy"
    capabilities = frozenset()


def _provider() -> DummyProvider:
    return DummyProvider()


def test_registry_rejects_duplicate_provider_keys() -> None:
    registry = ProviderRegistry()
    provider = DummyProvider()
    registry.register(provider)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(provider)  # type: ignore[arg-type]


def test_registry_rejects_a_duplicate_credential_account() -> None:
    registry = ProviderRegistry()
    registry.register_route("leaseweb", "lw-1", _provider())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="credential account already registered"):
        registry.register_route("leaseweb", "lw-1", _provider())  # type: ignore[arg-type]


def test_first_route_becomes_the_logical_adapter() -> None:
    registry = ProviderRegistry()
    first = _provider()
    second = _provider()
    registry.register_route("leaseweb", "lw-1", first)  # type: ignore[arg-type]
    registry.register_route("leaseweb", "lw-2", second)  # type: ignore[arg-type]

    assert registry.get("leaseweb") is first
    assert registry.get_for("leaseweb", "lw-2") is second
    assert registry.get_for("leaseweb", None) is first
    assert registry.route_ids("leaseweb") == ("lw-1", "lw-2")
    assert registry.has_routes("leaseweb") is True
    assert registry.has_routes("other") is False


def test_reserved_default_alias_keeps_legacy_rows_routable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Renaming accounts must not strand rows backfilled with ``default``."""
    registry = ProviderRegistry()
    logical = _provider()
    registry.register_route("leaseweb", "lw-eu", logical)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING):
        resolved = registry.get_for("leaseweb", DEFAULT_CREDENTIAL_ACCOUNT)

    assert resolved is logical
    assert "reserved" in caplog.text
    assert "lw-eu" in caplog.text


def test_unknown_account_fails_closed() -> None:
    registry = ProviderRegistry()
    registry.register_route("leaseweb", "lw-1", _provider())  # type: ignore[arg-type]

    with pytest.raises(UnknownCredentialAccountError) as excinfo:
        registry.get_for("leaseweb", "lw-removed")

    assert excinfo.value.account_id == "lw-removed"
    assert excinfo.value.provider_key == "leaseweb"


def test_single_credential_provider_accepts_any_account_id() -> None:
    """A provider without explicit routes predates credential accounts."""
    registry = ProviderRegistry()
    logical = _provider()
    registry.register(logical)  # type: ignore[arg-type]

    assert registry.get_for("dummy", "whatever") is logical
    assert registry.has_routes("dummy") is False


def test_unknown_provider_key_raises() -> None:
    registry = ProviderRegistry()
    with pytest.raises(KeyError):
        registry.get("nope")


def test_account_of_identifies_the_registered_adapter() -> None:
    registry = ProviderRegistry()
    first = _provider()
    second = _provider()
    registry.register_route("leaseweb", "lw-1", first)  # type: ignore[arg-type]
    registry.register_route("leaseweb", "lw-2", second)  # type: ignore[arg-type]

    assert registry.account_of(first) == "lw-1"  # type: ignore[arg-type]
    assert registry.account_of(second) == "lw-2"  # type: ignore[arg-type]
    assert registry.account_of(_provider()) is None  # type: ignore[arg-type]


def test_account_of_returns_none_for_a_provider_without_routes() -> None:
    registry = ProviderRegistry()
    logical = _provider()
    registry.register(logical)  # type: ignore[arg-type]

    assert registry.account_of(logical) is None  # type: ignore[arg-type]


def test_account_of_maps_a_legacy_logical_adapter_to_default() -> None:
    """A pre-multi-account adapter that stayed the logical one is "default"."""
    registry = ProviderRegistry()
    legacy = _provider()
    registry.register(legacy)  # type: ignore[arg-type]
    registry.register_route("dummy", "lw-1", _provider())  # type: ignore[arg-type]

    assert registry.get("dummy") is legacy
    assert registry.account_of(legacy) == DEFAULT_CREDENTIAL_ACCOUNT  # type: ignore[arg-type]


def test_account_views_are_safe_operator_metadata() -> None:
    registry = ProviderRegistry()
    view = CredentialAccountView(
        provider_key="leaseweb",
        account_id="lw-1",
        state=CredentialAccountState.DRAINING,
        priority=100,
        key_hint="ab12cd",
    )
    registry.register_account_views("leaseweb", (view,))

    assert registry.accounts("leaseweb") == (view,)
    assert registry.accounts("none") == ()
    assert view.enabled_for_new_orders is False
    assert view.usable is True
    assert view.ref == "leaseweb/lw-1"


def test_keys_are_sorted() -> None:
    registry = ProviderRegistry()
    registry.register_route("zeta", "lw-1", _provider())  # type: ignore[arg-type]
    registry.register_route("alpha", "lw-1", _provider())  # type: ignore[arg-type]

    assert registry.keys() == ("alpha", "zeta")
