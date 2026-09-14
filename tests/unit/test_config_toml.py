"""``configuration.toml`` loading, mapping and precedence (release hardening).

The runtime configuration contract is:

* the TOML file is the source of truth for runtime values;
* ``CLOUD_PLATFORM_CONFIG_FILE`` only points at the file (never a secret);
* environment variables keep precedence for bootstrap/test compatibility;
* a missing/invalid explicit file fails loudly instead of silently running
  with defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cloud_platform.core.config import (
    CONFIG_FILE_ENV,
    ConfigFileError,
    get_settings,
    load_settings,
    reset_settings_cache,
    resolve_config_file,
    toml_to_settings,
)

#: Repository root (tests run with the CWD moved to a temp dir).
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Isolate the process-wide settings singleton between tests."""
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from a directory with no configuration.toml and no bootstrap var."""
    monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)


def _write(tmp_path: Path, body: str, name: str = "configuration.toml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestMapping:
    """Nested TOML sections map onto the flat Settings contract."""

    def test_scalar_sections_map(self) -> None:
        mapped = toml_to_settings(
            {
                "app": {"environment": "production", "log_level": "WARNING"},
                "database": {"url": "postgresql+asyncpg://u:p@h:5432/db"},
                "redis": {"url": "redis://cache:6379/1"},
                "billing": {"price_book_name": "retail-eur", "low_balance_grace_hours": 6},
                "backup": {"output_dir": "/var/backups/x", "retention_days": 30},
            }
        )
        assert mapped["app_env"] == "production"
        assert mapped["log_level"] == "WARNING"
        assert mapped["database_url"] == "postgresql+asyncpg://u:p@h:5432/db"
        assert mapped["redis_url"] == "redis://cache:6379/1"
        assert mapped["price_book_name"] == "retail-eur"
        assert mapped["low_balance_grace_hours"] == 6
        assert mapped["backup_output_dir"] == "/var/backups/x"
        assert mapped["backup_retention_days"] == 30

    def test_unknown_sections_are_ignored(self) -> None:
        mapped = toml_to_settings({"nonsense": {"value": 1}, "app": {"note": "x"}})
        assert mapped == {}

    def test_telegram_logger_section_maps_every_flag(self) -> None:
        mapped = toml_to_settings(
            {
                "telegram": {
                    "bot_token": "token",
                    "logger": {
                        "enabled": True,
                        "chat_id": -1001234567890,
                        "log_purchases": False,
                        "log_recharges": True,
                        "log_payment_failures": False,
                        "log_order_failures": True,
                        "log_admin_wallet_adjustments": False,
                    },
                }
            }
        )
        assert mapped["telegram_bot_token"] == "token"
        assert mapped["telegram_logger_enabled"] is True
        assert mapped["telegram_logger_chat_id"] == -1001234567890
        assert mapped["telegram_logger_log_purchases"] is False
        assert mapped["telegram_logger_log_recharges"] is True
        assert mapped["telegram_logger_log_payment_failures"] is False
        assert mapped["telegram_logger_log_order_failures"] is True
        assert mapped["telegram_logger_log_admin_wallet_adjustments"] is False

    def test_provider_sections_build_the_storefront_metadata(self) -> None:
        mapped = toml_to_settings(
            {
                "providers": {
                    "leaseweb": {
                        "enabled": True,
                        "market": "Foreign",
                        "display_name": "Leaseweb",
                        "api_key": "lsw",  # pragma: allowlist secret
                        "locations": ["AMS-01", "FRA-01"],
                        "os_allowlist": [],
                        "order_os_only_free": True,
                    },
                    "arvancloud": {
                        "enabled": False,
                        "market": "iran",
                        "display_name": "ArvanCloud",
                        "api_key": "arv",  # pragma: allowlist secret
                        "region": "ir-thr-c1",
                    },
                }
            }
        )
        assert mapped["leaseweb_api_key"] == "lsw"  # pragma: allowlist secret
        assert mapped["leaseweb_locations"] == "AMS-01,FRA-01"
        assert mapped["leaseweb_os_allowlist"] == ""
        assert mapped["leaseweb_order_os_only_free"] is True
        assert mapped["arvancloud_api_key"] == "arv"  # pragma: allowlist secret
        assert mapped["arvancloud_region"] == "ir-thr-c1"
        # market metadata is normalized and provider-neutral (no branching)
        assert mapped["provider_markets"] == {"leaseweb": "foreign", "arvancloud": "iran"}
        assert mapped["provider_display_names"] == {
            "leaseweb": "Leaseweb",
            "arvancloud": "ArvanCloud",
        }
        assert mapped["providers_enabled"] == {"leaseweb": True, "arvancloud": False}

    def test_provider_api_token_alias(self) -> None:
        mapped = toml_to_settings({"providers": {"hetzner": {"api_token": "hz"}}})
        assert mapped["hetzner_api_token"] == "hz"

    def test_defaults_when_no_file_is_present(self) -> None:
        settings = load_settings(None)
        assert settings.app_env == "development"
        assert settings.provider_markets["arvancloud"] == "iran"
        assert settings.provider_markets["leaseweb"] == "foreign"
        assert settings.telegram_logger_enabled is False
        assert settings.telegram_logger_chat_id == 0


class TestFileResolution:
    """Which file is read — and what happens when it is missing or broken."""

    def test_dev_default_is_used_when_present(self, tmp_path: Path) -> None:
        _write(tmp_path, '[app]\nlog_level = "DEBUG"\n')
        assert load_settings(None).log_level == "DEBUG"

    def test_explicit_path_must_exist(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigFileError):
            load_settings(tmp_path / "nope.toml")

    def test_bootstrap_env_var_points_at_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Production requires the redis session backend (fail-closed
        # Settings contract), so the fixture file carries it explicitly.
        path = _write(
            tmp_path,
            '[app]\nenvironment = "production"\n[telegram.sessions]\nbackend = "redis"\n',
        )
        monkeypatch.setenv(CONFIG_FILE_ENV, str(path))
        assert resolve_config_file() == path
        assert load_settings(None).app_env == "production"

    def test_bootstrap_env_var_must_point_at_an_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONFIG_FILE_ENV, str(tmp_path / "missing.toml"))
        with pytest.raises(ConfigFileError):
            load_settings(None)

    def test_invalid_toml_fails_loudly(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "[app\nbroken")
        with pytest.raises(ConfigFileError):
            load_settings(path)

    def test_explicit_path_beats_env_bootstrap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chosen = _write(tmp_path, '[app]\nlog_level = "ERROR"\n', name="chosen.toml")
        other = _write(tmp_path, '[app]\nlog_level = "CRITICAL"\n', name="other.toml")
        monkeypatch.setenv(CONFIG_FILE_ENV, str(other))
        assert load_settings(chosen).log_level == "ERROR"


class TestPrecedence:
    """init > environment > TOML > .env > defaults, documented and enforced."""

    def test_toml_is_used_when_no_env_override_exists(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[providers.leaseweb]\napi_key = "from-toml"\n')
        assert load_settings(path).leaseweb_api_key == "from-toml"  # pragma: allowlist secret

    def test_environment_overrides_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write(tmp_path, '[providers.leaseweb]\napi_key = "from-toml"\n')
        monkeypatch.setenv("LEASEWEB_API_KEY", "from-env")
        assert load_settings(path).leaseweb_api_key == "from-env"  # pragma: allowlist secret

    def test_file_overrides_defaults_but_not_other_fields(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[app]\ndefault_currency = "USD"\n')
        settings = load_settings(path)
        assert settings.default_currency == "USD"
        assert settings.leaseweb_api_base_url == "https://api.leaseweb.com"

    def test_introspection_exposes_the_mapped_values(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[app]\nlog_level = "INFO"\n')
        assert load_settings(path).toml_values == {"log_level": "INFO"}

    def test_get_settings_is_cached_and_resettable(self, tmp_path: Path) -> None:
        _write(tmp_path, '[app]\nlog_level = "DEBUG"\n')
        assert get_settings() is get_settings()
        reset_settings_cache()
        assert get_settings().log_level == "DEBUG"


class TestCommittedExample:
    """The committed example must parse and stay secret-free."""

    def test_example_file_parses(self) -> None:
        example = REPO_ROOT / "configuration.example.toml"
        assert example.is_file(), "configuration.example.toml must ship in the repo"
        settings = load_settings(example)
        assert settings.leaseweb_api_base_url == "https://api.leaseweb.com"
        assert settings.leaseweb_locations == "AMS-01,FRA-01"
        assert settings.provider_markets["arvancloud"] == "iran"
        assert settings.telegram_logger_enabled is True
        assert settings.providers_enabled["hetzner"] is False

    def test_example_file_contains_no_real_secret(self) -> None:
        text = (REPO_ROOT / "configuration.example.toml").read_text(encoding="utf-8")
        # Every credential-shaped value is an obvious placeholder.
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            # An inline "# ..." comment is documentation, not part of the value.
            raw = value.split("#", 1)[0].strip().strip('"')
            if key.strip() in {
                "api_key",
                "api_token",
                "bot_token",
                "merchant_id",
                "callback_signing_key",
            }:
                assert raw in {"CHANGE_ME", "bot_token", ""}, f"{key} looks like a real secret"

    def test_configuration_toml_is_git_ignored(self) -> None:
        ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "configuration.toml" in ignored
        assert "!configuration.example.toml" in ignored
