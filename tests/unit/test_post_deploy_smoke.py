"""Tests for the post-deploy smoke test (M12-007).

Acceptance: health + safe read-only provider checks.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from scripts import post_deploy_smoke as smoke


def _fake_http(responses: dict[str, tuple[int, str]]) -> object:
    def fake(url: str, timeout: float = 5.0) -> tuple[int, str]:
        if url.rstrip("/").endswith(tuple(responses)):
            return responses[url.rstrip("/")]
        for suffix, (status, body) in responses.items():
            if url.endswith(suffix):
                return status, body
        return 404, "not found"

    return fake


OK = {
    "http://x/health/live": (200, json.dumps({"status": "ok"})),
    "http://x/health/ready": (200, json.dumps({"status": "ok", "mode": "starter"})),
    "http://x/metrics": (200, "cloud_platform_api_requests_total 1\n"),
}


class TestHealthChecks:
    def test_all_green(self) -> None:
        with patch.object(smoke, "http_get", _fake_http(OK)):
            results = smoke.check_health("http://x")
            metrics = smoke.check_metrics("http://x")
        assert [r.ok for r in results] == [True, True]
        assert metrics.ok

    def test_live_failure_fails(self) -> None:
        with patch.object(smoke, "http_get", _fake_http({"http://x/health/live": (500, "boom")})):
            results = smoke.check_health("http://x")
        assert results[0].ok is False

    def test_ready_wrong_status_fails(self) -> None:
        bad = {
            "http://x/health/live": (200, json.dumps({"status": "ok"})),
            "http://x/health/ready": (200, json.dumps({"status": "degraded"})),
        }
        with patch.object(smoke, "http_get", _fake_http(bad)):
            results = smoke.check_health("http://x")
        assert results[1].ok is False

    def test_connection_refused_fails(self) -> None:
        with patch.object(smoke, "http_get", lambda url, timeout=5.0: (0, "URLError: refused")):
            results = smoke.check_health("http://x")
        assert all(r.ok is False for r in results)

    def test_worker_check_uses_readiness(self) -> None:
        with patch.object(smoke, "http_get", _fake_http(OK)):
            assert smoke.check_worker("http://x").ok is True


class TestMigrationHead:
    def test_repo_head_is_the_latest_revision(self) -> None:
        head = smoke.repo_head_revision()
        assert head == "0020", f"expected 0020 to be the head, got {head!r}"

    def test_check_reports_expected_head(self) -> None:
        result = smoke.check_migration_head("http://x", None)
        assert result.ok
        assert "0020" in result.detail

    def test_check_with_override(self) -> None:
        result = smoke.check_migration_head("http://x", "0099")
        assert "0099" in result.detail


class TestProviderCheck:
    def test_no_token_skips_without_failure(self) -> None:
        result = smoke.check_provider_read_only("hetzner", None, None)
        assert result.ok is True
        assert "skipped" in result.detail

    def test_read_only_list_images(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        async def _images(self: object) -> list:
            return ["img-1", "img-2"]

        with patch.object(HetznerCloudProvider, "list_images", _images):
            result = smoke.check_provider_read_only("hetzner", "https://api.test", "tok")
        assert result.ok is True
        assert "images" in result.detail

    def test_provider_failure_fails_the_check(self) -> None:
        from cloud_platform.providers.hetzner.client import HetznerCloudProvider

        async def _boom(self: object) -> object:
            raise RuntimeError("401 unauthorized")

        with patch.object(HetznerCloudProvider, "list_images", _boom):
            result = smoke.check_provider_read_only("hetzner", "https://api.test", "tok")
        assert result.ok is False
        assert "FAILED" in result.detail


class TestMain:
    def test_main_passes_when_green(self, capsys: pytest.CaptureFixture) -> None:
        with patch.object(smoke, "http_get", _fake_http(OK)):
            code = smoke.main(["--base-url", "http://x"])
        out = capsys.readouterr().out
        assert code == 0
        assert "PASSED" in out
        assert "alembic current == head" in out

    def test_main_fails_when_red(self, capsys: pytest.CaptureFixture) -> None:
        dead = {
            "http://x/health/live": (0, "refused"),
            "http://x/health/ready": (0, "refused"),
        }
        with patch.object(smoke, "http_get", _fake_http(dead)):
            code = smoke.main(["--base-url", "http://x"])
        out = capsys.readouterr().out
        assert code == 1
        assert "FAILED" in out

    def test_main_provider_skipped_by_default(self, capsys: pytest.CaptureFixture) -> None:
        with patch.object(smoke, "http_get", _fake_http(OK)):
            code = smoke.main(["--base-url", "http://x"])
        out = capsys.readouterr().out
        assert "provider" not in out  # no provider check without --provider-key
        assert code == 0
