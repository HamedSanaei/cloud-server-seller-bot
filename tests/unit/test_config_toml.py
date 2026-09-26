"""``configuration.toml`` loading, mapping and precedence (release hardening).

The runtime configuration contract is:

* the TOML file is the source of truth for runtime values;
* ``CLOUD_PLATFORM_CONFIG_FILE`` only points at the file (never a secret);
* environment variables keep precedence for bootstrap/test compatibility;
* a missing/invalid explicit file fails loudly instead of silently running
  with defaults.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

# Private on purpose: this mapping IS the configuration contract the canonical
# template has to document, so the completeness test reads the same table the
# loader uses instead of duplicating a key list here.
from cloud_platform.core.config import (
    _TOML_FIELDS,
    CONFIG_FILE_ENV,
    TOML_LEGACY_ALIAS_KEYS,
    ConfigFileError,
    get_settings,
    load_settings,
    looks_corrupted_label,
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


#: Directories that never carry a configuration template.
_SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".ci-artifacts",
        "htmlcov",
    }
)

#: The ONE canonical template, relative to the repository root.
CANONICAL_EXAMPLE = "configuration.example.toml"

#: Contract keys deliberately NOT in the template: legacy aliases kept only so
#: an old server file keeps loading (the canonical spelling is documented
#: instead). Imported from the loader so the guard cannot drift from it.
LEGACY_ALIAS_KEYS = TOML_LEGACY_ALIAS_KEYS

#: Settings the production-only sections own, asserted individually below.
TETRAMINATOR_KEYS = (
    ("payments", "tetraminator", "enabled"),
    ("payments", "tetraminator", "api_key"),
    ("payments", "tetraminator", "base_url"),
    ("payments", "tetraminator", "callback_url"),
    ("payments", "tetraminator", "timeout_seconds"),
)


def _example_document() -> dict:
    return tomllib.loads((REPO_ROOT / CANONICAL_EXAMPLE).read_text(encoding="utf-8"))


def _nested(document: dict, path: tuple[str, ...]) -> tuple[bool, object]:
    node: object = document
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _configuration_examples() -> list[str]:
    """Every file in the working tree named ``configuration.example.toml``."""
    found: list[str] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = sorted(directory for directory in dirs if directory not in _SKIPPED_DIRS)
        for name in sorted(files):
            if name == "configuration.example.toml":
                found.append((Path(root) / name).relative_to(REPO_ROOT).as_posix())
    return found


class TestCommittedExample:
    """The committed example must parse and stay secret-free."""

    def test_example_file_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Explicit file, but environment still outranks TOML: drop the one
        # variable that switches validation into production-strict mode so
        # the test is deterministic on any machine.
        monkeypatch.delenv("APP_ENV", raising=False)
        example = REPO_ROOT / CANONICAL_EXAMPLE
        assert example.is_file(), "configuration.example.toml must ship in the repo"
        settings = load_settings(example)
        assert settings.leaseweb_api_base_url == "https://api.leaseweb.com"
        assert settings.leaseweb_locations == "AMS-01,FRA-01"
        assert settings.provider_markets["arvancloud"] == "iran"
        assert settings.telegram_logger_enabled is True
        assert settings.providers_enabled["hetzner"] is False
        # Tetraminator ships (placeholder-safe) and stays disabled by default.
        assert settings.tetraminator_enabled is False
        assert settings.tetraminator_api_key == "CHANGE_ME"  # pragma: allowlist secret
        assert settings.tetraminator_timeout_seconds == 30
        # The operator ids map from [telegram], never from [telegram.sessions].
        assert settings.telegram_admin_chat_id == 0
        assert settings.support_contact == "@support"
        # The catalog refresh budget (release-transition setting) is documented.
        assert settings.storefront_catalog_sync_timeout_seconds == 600
        assert settings.storefront_catalog_sync_interval_seconds == 900
        assert settings.storefront_pricing["leaseweb"]["markup_percent"] == 25

    def test_only_one_configuration_example_exists(self) -> None:
        """Exactly ONE canonical template — no per-environment copies.

        The deleted production-flavoured copy proved why: two templates drift,
        and the drift is invisible until a setting is missing in production.
        """
        assert _configuration_examples() == [CANONICAL_EXAMPLE]

    def test_no_live_reference_to_a_second_template(self) -> None:
        """No tracked document still points at a second example file."""
        stale = "deploy/production/configuration.example.toml"
        documents = (
            "README.md",
            "docs/operations/INSTALL.md",
            "docs/operations/PRODUCTION_DEPLOY.md",
            "docs/operations/RUNBOOK.md",
            "deploy/production/docker-compose.yml",
        )
        for name in documents:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            assert stale not in text, f"{name} still references {stale}"
        assert not (REPO_ROOT / "deploy/production/configuration.example.toml").exists()

    def test_template_documents_every_supported_key(self) -> None:
        """Completeness: the template and the parser cannot drift apart.

        Adding an operator-facing setting without documenting it in the
        canonical template fails here, which is the guard the duplicate example
        never provided.
        """
        document = _example_document()
        missing = [
            ".".join(path)
            for path in sorted(_TOML_FIELDS)
            if path not in LEGACY_ALIAS_KEYS and not _nested(document, path)[0]
        ]
        assert missing == [], f"undocumented configuration keys: {missing}"

    def test_telegram_operator_ids_are_not_nested_under_sessions(self) -> None:
        """The two operator ids belong to [telegram]; sessions owns bot state."""
        document = _example_document()
        assert _nested(document, ("telegram", "admin_chat_id"))[0] is True
        assert _nested(document, ("telegram", "support_contact"))[0] is True
        sessions = document["telegram"]["sessions"]
        assert "admin_chat_id" not in sessions
        assert "support_contact" not in sessions

    def test_tetraminator_section_is_documented(self) -> None:
        document = _example_document()
        missing = [".".join(path) for path in TETRAMINATOR_KEYS if not _nested(document, path)[0]]
        assert missing == [], f"missing payments.tetraminator keys: {missing}"
        _, block = _nested(document, ("payments", "tetraminator"))
        assert isinstance(block, dict)
        assert block["enabled"] is False
        assert str(block["callback_url"]).startswith("https://")

    def test_catalog_and_fx_keys_are_documented(self) -> None:
        """The current storefront/catalog/FX contract is in the template."""
        document = _example_document()
        for path in (
            ("storefront", "catalog_sync", "enabled"),
            ("storefront", "catalog_sync", "interval_seconds"),
            ("storefront", "catalog_sync", "timeout_seconds"),
            ("storefront", "catalog_sync", "fx_safety_margin_seconds"),
            ("fx", "catalog_pricing_currency"),
            ("fx", "global_enabled"),
            ("fx", "global_fiat_provider"),
            ("fx", "frankfurter", "catalog_max_stale_seconds"),
        ):
            assert _nested(document, path)[0] is True, f"missing {'.'.join(path)}"

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
        # The obsolete allowlist for the deleted production copy is gone: the
        # security model did not change, only the duplicated template did.
        assert "!deploy/**/configuration.example.toml" not in ignored

    def test_leaseweb_family_labels_are_real_customer_names(self) -> None:
        """The template ships the real Persian names, not placeholders.

        The production ``????`` labels came from a broken paste into the
        operator's own file, so the committed contract pins what a customer
        must read for the two commercial families.
        """
        document = _example_document()
        for family, expected in (("vps", "وی‌پی‌اس"), ("cloud", "کلود")):
            found, value = _nested(
                document, ("providers", "leaseweb", "families", family, "display_name")
            )
            assert found is True, f"providers.leaseweb.families.{family}.display_name missing"
            assert value == expected
            assert not looks_corrupted_label(value)


class TestCustomerFacingLabels:
    """A corrupted configured label must never become customer-facing text."""

    @pytest.mark.parametrize(
        "label",
        [
            "وی‌پی‌اس",
            "کلود",
            "Leaseweb",
            "General Purpose",
            "Compute Optimized",
            "  Hetzner  ",
            "میزبانی ۲",
        ],
    )
    def test_real_labels_are_accepted(self, label: str) -> None:
        assert looks_corrupted_label(label) is False

    @pytest.mark.parametrize("label", ["????????", "????", "? ? ?", "....", "\ufffd\ufffd"])
    def test_question_mark_garbage_is_rejected(self, label: str) -> None:
        assert looks_corrupted_label(label) is True

    @pytest.mark.parametrize("label", ["", "   ", None])
    def test_an_unset_label_is_not_corrupted(self, label: object) -> None:
        """Empty means "not configured": the caller applies its own fallback."""
        assert looks_corrupted_label(label) is False

    def test_catalog_never_renders_a_corrupted_name(self) -> None:
        from cloud_platform.modules.markets.domain import ProviderCatalog

        catalog = ProviderCatalog(
            markets={"leaseweb": "foreign"},
            display_names={"leaseweb": "????????"},
            families={
                "leaseweb": {
                    "vps": {"billing_model": "prepaid_monthly_fixed", "display_name": "????"},
                    "cloud": {"billing_model": "hourly", "display_name": "کلود"},
                }
            },
        )
        assert catalog.display_name_of("leaseweb") == "leaseweb"
        families = {f.family_key: f.display_name for f in catalog.families_of("leaseweb")}
        assert families == {"vps": "vps", "cloud": "کلود"}
        assert catalog.listing("leaseweb", ordering_capable=True).display_name == "leaseweb"  # type: ignore[union-attr]


class TestConfigDoctor:
    """`config doctor`: read-only drift detection, never a printed value.

    The duplicate template drifted silently because nothing compared the real
    file with the supported contract. The command reports BOTH directions
    (missing keys the loader defaults, unknown keys it ignores) and fails only
    on something that is really unusable: an unreadable/invalid file, a
    model-required key the file does not set, or an unreplaced placeholder in a
    production file.
    """

    def _point_at(self, monkeypatch: pytest.MonkeyPatch, path: Path | None) -> None:
        monkeypatch.setattr(
            "cloud_platform.core.config.resolve_config_file",
            lambda *args, **kwargs: path,
        )

    async def test_passes_on_the_canonical_template(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cloud_platform.cli as cli_module

        self._point_at(monkeypatch, REPO_ROOT / CANONICAL_EXAMPLE)
        assert await cli_module.config_doctor() == 0
        out = capsys.readouterr().out
        assert "config doctor: OK" in out
        # A development file may keep the documented placeholders...
        assert "placeholders (fine outside production)" in out
        # ...and every supported key is documented in the template itself.
        assert "missing keys (the Settings default applies)" not in out
        assert "unknown keys" not in out

    async def test_missing_and_unknown_keys_are_reported_without_values(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cloud_platform.cli as cli_module

        secret = "super-secret-value"  # pragma: allowlist secret (a fixture, not a credential)
        path = _write(
            tmp_path,
            f'[app]\nenvironment = "development"\nlog_level = "{secret}"\n'
            '[old]\nprovider = "foo"\n',
        )
        self._point_at(monkeypatch, path)
        assert await cli_module.config_doctor() == 0
        out = capsys.readouterr().out
        assert "missing keys (the Settings default applies):" in out
        assert "storefront.catalog_sync.timeout_seconds" in out
        assert "unknown keys" in out
        assert "old.provider" in out
        # Only key paths, never a value.
        assert secret not in out
        assert "config doctor: OK" in out

    async def test_corrupted_customer_facing_label_fails_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cloud_platform.cli as cli_module

        path = _write(
            tmp_path,
            '[app]\nenvironment = "development"\n\n'
            '[providers.leaseweb.families.vps]\ndisplay_name = "????????"\n\n'
            '[providers.leaseweb.families.cloud]\ndisplay_name = "کلود"\n',
        )
        self._point_at(monkeypatch, path)
        assert await cli_module.config_doctor() == 1
        out = capsys.readouterr().out
        assert "customer-facing labels that look corrupted" in out
        # The KEY PATH only: never the corrupted value, never a secret.
        assert "providers.leaseweb.families.vps.display_name" in out
        assert "????" not in out
        assert "config doctor: FAIL" in out

    async def test_real_customer_labels_do_not_fail_the_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cloud_platform.cli as cli_module

        path = _write(
            tmp_path,
            '[app]\nenvironment = "development"\n\n'
            '[providers.leaseweb]\nenabled = true\ndisplay_name = "Leaseweb"\n\n'
            '[providers.leaseweb.families.vps]\ndisplay_name = "وی‌پی‌اس"\n\n'
            '[providers.leaseweb.families.cloud]\ndisplay_name = "کلود"\n',
        )
        self._point_at(monkeypatch, path)
        assert await cli_module.config_doctor() == 0
        out = capsys.readouterr().out
        assert "customer-facing labels that look corrupted" not in out
        assert "config doctor: OK" in out

    async def test_unreplaced_placeholder_fails_a_production_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cloud_platform.cli as cli_module

        path = _write(
            tmp_path,
            '[app]\nenvironment = "production"\n[security]\nbackup_encryption_key = "CHANGE_ME"\n',
        )
        self._point_at(monkeypatch, path)
        assert await cli_module.config_doctor() == 1
        out = capsys.readouterr().out
        assert "placeholders that must be replaced:" in out
        assert "security.backup_encryption_key" in out
        assert "config doctor: FAIL" in out

    async def test_unreadable_or_missing_file_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cloud_platform.cli as cli_module
        from cloud_platform.core.config import ConfigFileError

        def _raise(*args: object, **kwargs: object) -> Path:
            raise ConfigFileError("configuration file not found: /nope.toml")

        monkeypatch.setattr("cloud_platform.core.config.resolve_config_file", _raise)
        assert await cli_module.config_doctor() == 1
        assert "config doctor: FAIL" in capsys.readouterr().out

        self._point_at(monkeypatch, None)
        assert await cli_module.config_doctor() == 1
        assert "no configuration file found" in capsys.readouterr().out

    async def test_invalid_toml_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import cloud_platform.cli as cli_module

        path = _write(tmp_path, "[app\nbroken")
        self._point_at(monkeypatch, path)
        assert await cli_module.config_doctor() == 1
        assert "not readable valid TOML" in capsys.readouterr().out
