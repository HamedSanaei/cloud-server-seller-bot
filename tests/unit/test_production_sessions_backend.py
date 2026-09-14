"""Production Telegram session backend invariant (release-owned compose).

The shared Redis transient-state backend is a NON-SECRET deployment topology
invariant enforced from the release-owned production compose
(``TELEGRAM_SESSIONS_BACKEND=redis`` in the shared ``x-platform`` block),
not by editing the server-owned ``configuration.toml``.

Layers (all three must agree):
1. compose injects the override into every platform process;
2. Settings precedence (environment > TOML) resolves ``redis`` even when a
   stale server TOML still says ``memory``;
3. Settings fail closed: production + effective ``memory`` raises instead of
   silently running on process-local state.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
PROD_COMPOSE = REPO_ROOT / "deploy" / "production" / "docker-compose.yml"

#: Secret names that must never gain a literal value in production compose.
_SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "CALLBACK_SIGNING_KEY",
    "HETZNER_API_TOKEN",
    "LEASEWEB_API_KEY",
    "ARVANCLOUD_API_KEY",
    "ZARINPAL_MERCHANT_ID",
    "POSTGRES_PASSWORD",
    "PROVIDER_CREDENTIAL_ENCRYPTION_KEY",
    "BACKUP_ENCRYPTION_KEY",
)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bare Settings() must see pure defaults, never a developer checkout."""
    from cloud_platform.core.config import CONFIG_FILE_ENV

    monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
    monkeypatch.chdir(tmp_path)


def _platform_environment() -> dict:
    compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
    return compose["x-platform"]["environment"]


class TestComposeInjectsRedisBackend:
    def test_shared_block_sets_redis(self) -> None:
        assert _platform_environment()["TELEGRAM_SESSIONS_BACKEND"] == "redis"

    def test_bootstrap_variable_is_kept(self) -> None:
        assert (
            _platform_environment()["CLOUD_PLATFORM_CONFIG_FILE"]
            == "/etc/cloud-server-seller/configuration.toml"
        )

    @pytest.mark.parametrize("service", ["api", "worker", "bot", "migrate"])
    def test_platform_services_receive_it(self, service: str) -> None:
        compose = yaml.safe_load(PROD_COMPOSE.read_text(encoding="utf-8"))
        environment = compose["services"][service]["environment"]
        assert environment["TELEGRAM_SESSIONS_BACKEND"] == "redis"

    def test_no_secret_moved_into_compose(self) -> None:
        text = PROD_COMPOSE.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for name in _SECRET_ENV_NAMES:
                match = re.search(rf"\b{name}\b\s*[:=]", stripped)
                if match is None:
                    continue
                # A mapping/assignment of a secret name is acceptable only
                # as a value-less pass-through reference (e.g. the
                # ${POSTGRES_PASSWORD:?...} database wiring), never a literal.
                value = stripped[match.end() :].strip()
                assert value.startswith("$"), (
                    f"{name} must not gain a literal value in production compose: {stripped}"
                )


class TestSettingsResolution:
    def test_stale_toml_memory_loses_to_compose_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Environment beats TOML: the exact production incident shape."""
        from cloud_platform.core.config import CONFIG_FILE_ENV, load_settings

        path = tmp_path / "configuration.toml"
        path.write_text(
            '[app]\nenvironment = "production"\n[telegram.sessions]\nbackend = "memory"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(CONFIG_FILE_ENV, str(path))
        monkeypatch.setenv("TELEGRAM_SESSIONS_BACKEND", "redis")
        assert load_settings(path).telegram_sessions_backend == "redis"

    def test_production_with_effective_memory_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.core.config import Settings

        monkeypatch.delenv("TELEGRAM_SESSIONS_BACKEND", raising=False)
        with pytest.raises(ValidationError, match="must be 'redis'"):
            Settings(app_env="production", telegram_sessions_backend="memory")

    def test_production_with_redis_is_accepted(self) -> None:
        from cloud_platform.core.config import Settings

        settings = Settings(app_env="production", telegram_sessions_backend="redis")
        assert settings.telegram_sessions_backend == "redis"

    def test_backend_names_are_canonicalized(self) -> None:
        from cloud_platform.core.config import Settings

        assert (
            Settings(app_env="staging", telegram_sessions_backend="Redis").telegram_sessions_backend
            == "redis"
        )

    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_development_and_test_keep_memory(self, environment: str) -> None:
        from cloud_platform.core.config import Settings

        settings = Settings(app_env=environment)
        assert settings.telegram_sessions_backend == "memory"


class TestDeploymentUntouchedServerConfig:
    def test_deploy_script_never_writes_configuration_toml(self) -> None:
        script = (REPO_ROOT / "scripts" / "deploy-production.sh").read_text(encoding="utf-8")
        for line in script.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "CONFIGURATION_PATH}" not in stripped.split(">", 1)[-1] or ">" not in stripped, (
                f"deploy must never redirect into configuration.toml: {stripped}"
            )

    def test_release_compose_flow_still_intact(self) -> None:
        script = (REPO_ROOT / "scripts" / "deploy-production.sh").read_text(encoding="utf-8")
        assert "compose_candidate() {" in script
        assert "compose_current() {" in script
        assert "release compose promoted:" in script
