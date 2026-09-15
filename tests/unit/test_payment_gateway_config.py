"""Production payment-gateway configuration invariant (PROD-HARDENING).

An ENABLED gateway must be fully usable, and in production the Tetraminator
callback must be an absolute public ``https://`` URL. Catching this at the
configuration boundary is what lets the deployment preflight fail BEFORE any
mutation instead of surfacing the mistake when a customer presses "pay".

The adapter keeps its own runtime check as defence in depth (see
``test_tetraminator_gateway.py``); these tests cover the Settings layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cloud_platform.core.config import Settings

#: Recognisable fake key: it must never appear in an error message.
FAKE_KEY = "tetra_TEST_SUPER_SECRET_API_KEY"

#: The placeholder the committed examples ship with the gateway switched off.
PLACEHOLDER = "CHANGE_ME"

_PRODUCTION = {
    "app_env": "production",
    # Production also requires the shared Redis session backend, so isolate the
    # payments invariant from that (already-tested) one.
    "telegram_sessions_backend": "redis",
}


def _enabled(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tetraminator_enabled": True,
        "tetraminator_api_key": FAKE_KEY,
        "tetraminator_base_url": "https://api.tetraminator.com/v1",
        "tetraminator_callback_url": "https://pay.example.test/webhooks/payments/tetraminator",
    }
    base.update(overrides)
    return base


def test_production_https_callback_is_valid() -> None:
    settings = Settings(**_PRODUCTION, **_enabled())

    assert settings.tetraminator_enabled is True
    assert settings.tetraminator_callback_url.startswith("https://")


@pytest.mark.parametrize(
    "overrides, expected",
    [
        (
            {"tetraminator_callback_url": "http://pay.example.test/webhooks/tetraminator"},
            "https://",
        ),
        ({"tetraminator_callback_url": ""}, "absolute http(s) URL"),
        ({"tetraminator_callback_url": "/webhooks/payments/tetraminator"}, "absolute http(s) URL"),
        ({"tetraminator_callback_url": "pay.example.test/webhooks"}, "absolute http(s) URL"),
        ({"tetraminator_api_key": ""}, "api_key is required"),
        ({"tetraminator_api_key": "   "}, "api_key is required"),
        ({"tetraminator_base_url": "api.tetraminator.com/v1"}, "base_url must be an absolute"),
        ({"tetraminator_base_url": ""}, "base_url must be an absolute"),
        ({"tetraminator_timeout_seconds": 0}, "greater than 0"),
    ],
)
def test_production_enabled_gateway_must_be_usable(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(**_PRODUCTION, **_enabled(**overrides))

    assert expected in str(excinfo.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tetraminator_callback_url": "http://pay.example.test/webhooks/tetraminator"},
        {"tetraminator_callback_url": ""},
        {"tetraminator_api_key": ""},
        {"tetraminator_base_url": "not-a-url"},
    ],
)
def test_validation_errors_never_contain_the_api_key(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(**_PRODUCTION, **_enabled(**overrides))

    assert FAKE_KEY not in str(excinfo.value)
    assert FAKE_KEY not in repr(excinfo.value)


def test_disabled_gateway_tolerates_placeholders() -> None:
    """The committed example ships placeholders with the gateway switched off."""
    settings = Settings(
        **_PRODUCTION,
        tetraminator_enabled=False,
        tetraminator_api_key=PLACEHOLDER,
        tetraminator_callback_url="https://your-domain.example/webhooks/payments/tetraminator",
    )

    assert settings.tetraminator_enabled is False
    # Nothing is validated while the gateway is off, and nothing was altered.
    assert settings.tetraminator_api_key == PLACEHOLDER


def test_development_may_use_an_http_callback() -> None:
    """Local/testing deployments have no public HTTPS edge."""
    settings = Settings(
        app_env="development",
        **_enabled(
            tetraminator_callback_url="http://127.0.0.1:8000/webhooks/payments/tetraminator"
        ),
    )

    assert settings.app_env == "development"


def test_environment_is_matched_case_insensitively() -> None:
    with pytest.raises(ValidationError):
        Settings(
            **{**_PRODUCTION, "app_env": "  Production  "},
            **_enabled(tetraminator_callback_url="http://pay.example.test/hook"),
        )


def test_toml_section_is_validated(tmp_path: Path) -> None:
    """The deploy preflight loads the server-owned TOML through this path."""
    config = tmp_path / "configuration.toml"
    config.write_text(
        '[app]\nenvironment = "production"\n'
        '[telegram.sessions]\nbackend = "redis"\n'
        "[payments.tetraminator]\n"
        "enabled = true\n"
        f'api_key = "{FAKE_KEY}"\n'  # pragma: allowlist secret
        'base_url = "https://api.tetraminator.com/v1"\n'
        'callback_url = "http://pay.example.test/webhooks/payments/tetraminator"\n'
        "timeout_seconds = 30\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError) as excinfo:
        Settings.model_validate_toml(config)

    assert "callback_url" in str(excinfo.value)
    assert FAKE_KEY not in str(excinfo.value)
