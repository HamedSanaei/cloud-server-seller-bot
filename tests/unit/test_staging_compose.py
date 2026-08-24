"""Tests for the staging compose deployment (M12-004).

Acceptance: staging deploy repeatable.

The compose file is the deployment artifact: the tests pin its structure so
a regression (someone dropping the migrate-first ordering, publishing the
database port, hard-coding a secret, or losing the healthchecks) is caught
at review time, not in staging.
"""

from __future__ import annotations

from pathlib import Path

import yaml

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "staging"


def _compose() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


class TestServiceGraph:
    def test_expected_services(self) -> None:
        services = _compose()["services"]
        for name in ("postgres", "redis", "migrate", "api", "worker", "backup", "volume-init"):
            assert name in services, f"missing service {name}"

    def test_migrations_run_before_api_and_worker(self) -> None:
        services = _compose()["services"]
        for name in ("api", "worker"):
            deps = services[name]["depends_on"]
            assert deps["migrate"]["condition"] == "service_completed_successfully", (
                f"{name} must wait for migrate to succeed (migration-first deploys)"
            )
            assert deps["postgres"]["condition"] == "service_healthy"
            assert deps["redis"]["condition"] == "service_healthy"

    def test_migrate_is_one_shot(self) -> None:
        services = _compose()["services"]
        assert services["migrate"]["restart"] == "no"
        assert services["migrate"]["command"] == ["migrate"]
        assert services["migrate"].get("ports") is None, "migrate must not publish ports"

    def test_api_worker_use_entrypoint_commands(self) -> None:
        services = _compose()["services"]
        assert services["api"]["command"] == ["api"]
        assert services["worker"]["command"] == ["worker"]

    def test_backup_is_profile_gated_and_one_shot(self) -> None:
        services = _compose()["services"]
        backup = services["backup"]
        assert "backup" in backup["profiles"]
        assert backup["restart"] == "no"
        assert backup["command"] == ["backup"]
        assert backup.get("ports") is None
        # the root-owned volume is chowned by the one-shot volume-init first
        assert backup["depends_on"]["volume-init"]["condition"] == "service_completed_successfully"
        init = services["volume-init"]
        assert init["user"] == "root"
        assert init["restart"] == "no"
        assert "chown" in " ".join(init["command"])


class TestImageAndConfig:
    def test_platform_services_use_the_release_image_variable(self) -> None:
        services = _compose()["services"]
        for name in ("migrate", "api", "worker", "backup", "volume-init"):
            image = services[name]["image"]
            assert "PLATFORM_IMAGE" in image, f"{name} must use the PLATFORM_IMAGE variable"

    def test_infrastructure_images_are_pinned(self) -> None:
        services = _compose()["services"]
        assert ":" in services["postgres"]["image"]
        assert ":" in services["redis"]["image"]

    def test_database_port_is_not_published(self) -> None:
        services = _compose()["services"]
        assert services["postgres"].get("ports") is None, "postgres must stay internal"
        assert services["redis"].get("ports") is None, "redis must stay internal"

    def test_api_publishes_a_configurable_port(self) -> None:
        api = _compose()["services"]["api"]
        assert api["ports"] == ["${STAGING_API_PORT:-8000}:8000"]

    def test_state_lives_in_named_volumes(self) -> None:
        data = _compose()
        assert set(data["volumes"]) == {"pgdata", "redisdata", "backups"}
        services = data["services"]
        assert "pgdata:/var/lib/postgresql/data" in services["postgres"]["volumes"]
        assert "redisdata:/data" in services["redis"]["volumes"]
        assert "backups:/backups" in services["backup"]["volumes"]

    def test_healthchecks_present_on_infra(self) -> None:
        services = _compose()["services"]
        assert "healthcheck" in services["postgres"]
        assert "healthcheck" in services["redis"]

    def test_env_wiring(self) -> None:
        services = _compose()["services"]
        env = services["api"]["environment"]
        assert env["APP_ENV"] == "staging"
        assert env["DATABASE_URL"].startswith("postgresql+asyncpg://cloud:")
        assert env["REDIS_URL"] == "redis://redis:6379/0"
        assert "STAGING_DB_PASSWORD" in env["DATABASE_URL"]  # from .env, not hard-coded
        worker_env = services["worker"]["environment"]
        assert worker_env == env or set(worker_env) == set(env)


class TestNoSecretsInRepo:
    """Nothing secret may be hard-coded in the compose file or .env.example:
    every secret arrives via a ${VAR} reference."""

    def test_no_literal_passwords_or_tokens(self) -> None:
        compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        for secret in ("sup3r", "Bearer ", "token=", "AKIA", "ghp_", "xoxb-"):
            assert secret not in compose
        # every env value that looks like a credential is a variable reference
        for service in _compose()["services"].values():
            for key, value in (service.get("environment") or {}).items():
                if any(m in key for m in ("PASSWORD", "TOKEN", "KEY")):
                    assert isinstance(value, str) and "${" in value, (
                        f"{key} must come from the environment, not a literal"
                    )

    def test_env_example_covers_required_variables(self) -> None:
        example = (DEPLOY / ".env.example").read_text(encoding="utf-8")
        required = [
            "PLATFORM_IMAGE",
            "STAGING_DB_PASSWORD",
            "STAGING_TELEGRAM_BOT_TOKEN",
            "STAGING_HETZNER_API_TOKEN",
            "STAGING_PROVIDER_CREDENTIAL_KEY",
            "STAGING_BACKUP_ENCRYPTION_KEY",
        ]
        for var in required:
            assert f"{var}=" in example, f"{var} missing from .env.example"
        assert "change-me" in example  # the placeholder is explicit, not a real secret


class TestReadme:
    def test_readme_documents_the_repeatable_loop(self) -> None:
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        for token in ("docker compose up -d", "release_oci_image.py", "health/live", "down"):
            assert token in readme
