"""Tests for the production deployment runbook (M12-005).

Acceptance: secrets, migrations, rollback and health checks documented.
"""

from __future__ import annotations

from pathlib import Path

RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "operations" / "PRODUCTION_RUNBOOK.md"


def _text() -> str:
    assert RUNBOOK.is_file(), "docs/operations/PRODUCTION_RUNBOOK.md missing"
    return RUNBOOK.read_text(encoding="utf-8")


class TestAcceptanceAreas:
    def test_secrets_are_documented(self) -> None:
        text = _text()
        # every production secret is named and its protection mechanism stated
        for token in (
            "DB_PASSWORD",
            "TELEGRAM_BOT_TOKEN",
            "HETZNER_API_TOKEN",
            "PROVIDER_CREDENTIAL_KEY",
            "BACKUP_ENCRYPTION_KEY",
        ):
            assert token in text, f"{token} missing from the secrets section"
        # the backup key is pinned to its backups (retention coupling)
        assert "retention" in text.lower()

    def test_migrations_are_documented(self) -> None:
        text = _text()
        assert "alembic" in text
        # migration-first ordering is explicit
        assert "before" in text.lower() or "migrate" in text
        # the destructive-change procedure is a two-phase rollout with an override
        assert "two-phase" in text.lower()
        assert "compat-override" in text
        # the gate is the enforcement point
        assert "compatibility gate" in text.lower() or "M12-003" in text

    def test_rollback_is_documented(self) -> None:
        text = _text()
        # code-first rollback via the immutable tag
        assert "PLATFORM_IMAGE" in text
        assert "SHA" in text or "sha" in text
        # schema rollback is the rare, considered path
        assert "downgrade" in text
        # and it never touches financial data
        assert "append-only" in text

    def test_health_checks_are_documented(self) -> None:
        text = _text()
        for token in ("/health/live", "/health/ready", "/metrics", "alembic current"):
            assert token in text, f"{token} missing from the health section"
        # the provider check must be explicitly read-only
        assert "read-only" in text.lower()

    def test_deploy_checklist_exists(self) -> None:
        text = _text()
        assert "checklist" in text.lower()
        # the checklist references the smoke test and the rollback target
        assert "smoke" in text.lower()
        assert "rollback" in text.lower()
