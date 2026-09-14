"""Regression tests for production Alembic configuration safety.

A migration must never silently fall back to the localhost placeholder
database when the operator explicitly configured ``CLOUD_PLATFORM_CONFIG_FILE``
and that file cannot be loaded — migrating the wrong database is worse than
no migration at all.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_SOURCE = (REPO_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")

CONFIG_FILE_ENV = "CLOUD_PLATFORM_CONFIG_FILE"
LOCALHOST_FALLBACK = "postgresql://cloud:cloud@localhost:5432/cloud"  # pragma: allowlist secret


def _settings_with_url(url: str) -> Any:
    """A minimal settings double exposing only ``database_url``."""
    return SimpleNamespace(database_url=url)


def _exec_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Execute alembic/env.py with stubbed alembic modules; return configured URLs."""
    from contextlib import contextmanager

    configured: list[dict[str, Any]] = []

    @contextmanager
    def _transaction():  # type: ignore[no-untyped-def]
        yield

    context_stub = types.ModuleType("alembic.context")
    context_stub.is_offline_mode = lambda: True
    context_stub.configure = lambda **kwargs: configured.append(kwargs) or None
    context_stub.begin_transaction = _transaction
    context_stub.run_migrations = lambda: None

    class _StubConfig:
        config_ini_section = "alembic"

        @staticmethod
        def get_main_option(name: str) -> str:
            # Mirrors the placeholder in alembic.ini (never a real database).
            return "driver://user:pass@localhost/dbname"  # pragma: allowlist secret

        @staticmethod
        def get_section(section: str, default: Any = None) -> dict[str, Any]:
            return {}

    context_stub.config = _StubConfig()
    alembic_stub = types.ModuleType("alembic")
    alembic_stub.context = context_stub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alembic", alembic_stub)
    monkeypatch.setitem(sys.modules, "alembic.context", context_stub)

    namespace: dict[str, Any] = {"__name__": "alembic_test_env", "__file__": "env.py"}
    exec(compile(ENV_SOURCE, "alembic/env.py", "exec"), namespace)
    return {"configured": configured, "db_url": namespace["DB_URL"]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield


class TestExplicitConfigFailure:
    def test_missing_explicit_toml_fails_loudly_not_localhost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The production incident: unreadable explicit TOML must not migrate localhost."""
        from cloud_platform.core.config import ConfigFileError

        def _broken() -> Any:
            raise ConfigFileError("configuration file not found: /etc/gone.toml")

        monkeypatch.setattr("cloud_platform.core.config.get_settings", _broken)
        monkeypatch.setenv(CONFIG_FILE_ENV, "/etc/gone.toml")
        with pytest.raises(RuntimeError, match="cannot load explicitly configured file"):
            _exec_env(monkeypatch)

    def test_error_names_the_file_but_never_a_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cloud_platform.core.config import ConfigFileError

        def _broken() -> Any:
            raise ConfigFileError("boom")

        monkeypatch.setattr("cloud_platform.core.config.get_settings", _broken)
        monkeypatch.setenv(CONFIG_FILE_ENV, "/etc/cloud-server-seller/configuration.toml")
        with pytest.raises(RuntimeError) as excinfo:
            _exec_env(monkeypatch)
        assert "/etc/cloud-server-seller/configuration.toml" in str(excinfo.value)
        assert "cloud:cloud" not in str(excinfo.value)


class TestNonExplicitBehaviorPreserved:
    def test_valid_explicit_toml_uses_its_database_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings_with_url(
            "postgresql+asyncpg://cloud:s3cret@db:5432/cloud",  # pragma: allowlist secret
        )
        monkeypatch.setattr("cloud_platform.core.config.get_settings", lambda: settings)
        monkeypatch.setenv(CONFIG_FILE_ENV, "/etc/cloud-server-seller/configuration.toml")
        result = _exec_env(monkeypatch)
        # The async driver prefix is stripped for synchronous migrations.
        expected = "postgresql://cloud:s3cret@db:5432/cloud"  # pragma: allowlist secret
        assert result["db_url"] == expected
        assert result["configured"][0]["url"] == expected

    def test_no_explicit_file_keeps_local_dev_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> Any:
            raise RuntimeError("no configuration in this dev checkout")

        monkeypatch.setattr("cloud_platform.core.config.get_settings", _broken)
        monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
        result = _exec_env(monkeypatch)
        assert result["db_url"] == LOCALHOST_FALLBACK

    def test_database_url_env_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _must_not_run() -> Any:
            raise AssertionError("settings must not be consulted")

        monkeypatch.setattr("cloud_platform.core.config.get_settings", _must_not_run)
        monkeypatch.delenv(CONFIG_FILE_ENV, raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql://envUser@envhost/envdb")
        result = _exec_env(monkeypatch)
        assert result["db_url"] == "postgresql://envUser@envhost/envdb"
