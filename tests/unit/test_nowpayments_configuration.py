"""Hermetic NOWPayments config, creation policy and transport ownership checks."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from cloud_platform.core import config as configuration
from cloud_platform.core import container as wiring
from cloud_platform.core.config import Settings
from cloud_platform.core.container import Container

FIXTURE = {
    "nowpayments_enabled": True,
    "nowpayments_api_key": "np-config-fixture-api-key",
    "nowpayments_auth_email": "fixture@merchant.test",
    "nowpayments_auth_password": "np-config-strong-todo-password",
    "nowpayments_ipn_secret": "np-config-fixture-ipn-secret",
    "nowpayments_base_url": "https://api.nowpayments.io/v1",
    "nowpayments_callback_url": "https://app.test/webhooks/payments/nowpayments",
    "nowpayments_timeout_seconds": 17.5,
}


@pytest.fixture(autouse=True)
def hermetic_settings(monkeypatch, tmp_path):
    """Never read private TOML, dotenv or ambient credential values."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setattr(configuration, "_ACTIVE_CONFIG_FILE", None)

    def sources(cls, settings_cls, init_settings, **ignored):
        path = configuration._ACTIVE_CONFIG_FILE
        if path is None:
            return (init_settings,)
        assert path.is_relative_to(tmp_path)
        return (
            init_settings,
            configuration._TomlSettingsSource(settings_cls, configuration.parse_config_file(path)),
        )

    monkeypatch.setattr(Settings, "settings_customise_sources", classmethod(sources))


def settings(**overrides):
    return Settings(**(FIXTURE | overrides))


def test_nowpayments_defaults_are_disabled_and_secret_values_are_not_represented():
    defaults = Settings()
    assert defaults.nowpayments_enabled is False
    assert defaults.nowpayments_api_key == defaults.nowpayments_auth_password == ""
    assert defaults.nowpayments_ipn_secret == ""
    assert defaults.nowpayments_base_url == "https://api.nowpayments.io/v1"
    assert defaults.nowpayments_callback_url == ""
    assert defaults.nowpayments_timeout_seconds == 30
    represented = repr(settings())
    for key in ("api_key", "auth_email", "auth_password", "ipn_secret"):
        assert FIXTURE[f"nowpayments_{key}"] not in represented


def test_all_exact_toml_keys_map_and_load_from_an_explicit_fixture(tmp_path):
    section = {key.removeprefix("nowpayments_"): value for key, value in FIXTURE.items()}
    assert configuration.toml_to_settings({"payments": {"nowpayments": section}}) == FIXTURE
    body = "[payments.nowpayments]\n" + "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in section.items()
    )
    path = tmp_path / "nowpayments-fixture.toml"
    path.write_text(body, encoding="utf-8")
    loaded = Settings.model_validate_toml(path)
    for key, value in FIXTURE.items():
        assert getattr(loaded, key) == value


def test_committed_example_documents_every_nowpayments_setting():
    example = Path(__file__).resolve().parents[2] / "configuration.example.toml"
    section = tomllib.loads(example.read_text(encoding="utf-8"))["payments"]["nowpayments"]
    assert set(section) == {key.removeprefix("nowpayments_") for key in FIXTURE}
    assert section["enabled"] is False
    assert section["callback_url"].endswith("/webhooks/payments/nowpayments")


@pytest.mark.parametrize("key", ["api_key", "auth_email", "auth_password", "ipn_secret"])
@pytest.mark.parametrize("invalid", ["", "CHANGE_ME", "<replace-this>"])
def test_enabled_requires_complete_nonplaceholder_credentials(key, invalid):
    with pytest.raises(ValidationError) as raised:
        settings(**{f"nowpayments_{key}": invalid})
    assert key in str(raised.value)
    for secret in ("api_key", "auth_password", "ipn_secret"):
        assert FIXTURE[f"nowpayments_{secret}"] not in str(raised.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("auth_email", "not-an-email"),
        ("callback_url", "https://app.test/webhooks/payments/nowpayments/ipn"),
        ("callback_url", "https://app.test/webhooks/payments/atlaspay"),
        ("callback_url", "http://app.test/webhooks/payments/nowpayments"),
        ("callback_url", "https://name:password@app.test/webhooks/payments/nowpayments"),
        ("callback_url", "https://app.test/webhooks/payments/nowpayments?token=invalid"),
        ("callback_url", "https://app.test/webhooks/payments/nowpayments#fragment"),
        ("base_url", "http://api.nowpayments.io/v1"),
        ("base_url", "https://name:password@api.nowpayments.io/v1"),
        ("base_url", "https://api.nowpayments.io/v1?token=invalid"),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
        ("timeout_seconds", float("nan")),
        ("timeout_seconds", float("inf")),
    ],
)
def test_unsafe_configuration_fails_before_any_provider_request(key, value):
    with pytest.raises(ValidationError):
        settings(**{f"nowpayments_{key}": value})


def test_disabled_example_may_retain_placeholders():
    disabled = settings(
        nowpayments_enabled=False,
        nowpayments_api_key="CHANGE_ME",
        nowpayments_auth_password="CHANGE_ME",
        nowpayments_ipn_secret="CHANGE_ME",
    )
    assert disabled.nowpayments_enabled is False


@pytest.mark.parametrize("override", [None, True, False])
async def test_disabled_with_valid_credentials_remains_registered_but_creation_obeys_policy(
    monkeypatch, override
):
    configured = settings(nowpayments_enabled=False)
    monkeypatch.setattr(wiring, "get_settings", lambda: configured)
    built = object.__new__(Container)
    object.__setattr__(built, "session_factory", None)
    monkeypatch.setattr(Container, "business_event_sink", lambda self: None)
    monkeypatch.setattr(Container, "user_repository", lambda self: None)
    policy = SimpleNamespace(
        get=AsyncMock(return_value=override),
        list=AsyncMock(return_value={} if override is None else {"nowpayments": override}),
    )
    monkeypatch.setattr(Container, "gateway_settings_repository", lambda self: policy)
    gateways = built.payment_gateways()
    try:
        assert "nowpayments" in gateways
        assert built.payment_gateway_defaults()["nowpayments"] is False
        recharge = built.wallet_recharge_service(gateways=gateways, fx_resolver=object())
        expected = ["nowpayments"] if override is True else []
        assert await recharge.compatible_gateways_async("USD", 1000) == expected
        administration = built.gateway_management_service(gateways)
        administration._require_admin = AsyncMock(return_value=uuid4())
        states = {item.key: item.enabled for item in await administration.list(object())}
        assert states["nowpayments"] is (override is True)
    finally:
        await built.aclose_gateways(gateways)


@pytest.mark.parametrize("consumer_fails", [False, True])
async def test_native_dependency_closes_scoped_transports_even_when_consumer_fails(
    monkeypatch, consumer_fails
):
    close = AsyncMock()
    gateway = SimpleNamespace(key="nowpayments", close=close)
    container = SimpleNamespace(
        session_factory=None,
        payment_gateways=lambda: {"nowpayments": gateway},
        aclose_gateways=Container.aclose_gateways,
    )
    monkeypatch.setattr(wiring, "get_container", AsyncMock(return_value=container))
    monkeypatch.setattr(wiring, "get_payment_webhook_service", AsyncMock(return_value=object()))
    dependency = wiring.get_nowpayments_callback_service()
    await anext(dependency)
    close.assert_not_awaited()
    if consumer_fails:
        with pytest.raises(RuntimeError, match="fixture consumer error"):
            await dependency.athrow(RuntimeError("fixture consumer error"))
    else:
        await dependency.aclose()
    close.assert_awaited_once()


async def test_registered_adapter_unit_test_uses_mock_transport_only():
    """The constructor itself never contacts the provider."""
    from cloud_platform.providers.nowpayments import NowPaymentsGateway

    gateway = NowPaymentsGateway(
        FIXTURE["nowpayments_api_key"],
        callback_url=FIXTURE["nowpayments_callback_url"],
        auth_email=FIXTURE["nowpayments_auth_email"],
        auth_password=FIXTURE["nowpayments_auth_password"],
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    await gateway.close()
