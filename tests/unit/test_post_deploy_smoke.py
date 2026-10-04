"""Tests for the post-deploy smoke test (M12-007).

Acceptance: health + safe read-only provider checks.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from cloud_platform.core.config import HetznerAccountSettings, Settings
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
    def test_repo_head_is_a_single_well_formed_revision(self) -> None:
        """Derived from the chain, not pinned to a number.

        The smoke test already computes the head by walking the migrations, so
        pinning the literal here only meant every new revision needed two edits
        — and a stale pin would have claimed a head the image does not ship.
        """
        head = smoke.repo_head_revision()
        assert head, "the checkout has no alembic head"
        assert "," not in head, f"the migration chain has several heads: {head}"
        assert head.isdigit() and len(head) == 4, head

    def test_check_reports_expected_head(self) -> None:
        result = smoke.check_migration_head("http://x", None)
        assert result.ok
        assert smoke.repo_head_revision() in result.detail

    def test_check_with_override(self) -> None:
        result = smoke.check_migration_head("http://x", "0099")
        assert "0099" in result.detail


class TestProviderCheck:
    @staticmethod
    def _transport(
        monkeypatch: pytest.MonkeyPatch,
        *,
        fail: bool = False,
    ) -> list[httpx.Request]:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.method == "GET", "smoke diagnostics must remain read-only"
            if fail:
                return httpx.Response(
                    401,
                    json={
                        "error": {
                            "code": "unauthorized",
                            "message": "private-token-diagnostic",
                        }
                    },
                )
            key = request.url.path.rsplit("/", 1)[-1]
            if key == "images":
                values = [{"id": 1, "name": "ubuntu-24.04"}]
            elif key == "servers":
                values = [
                    {"id": 1, "status": "off", "labels": {}},
                    {"id": 2, "status": "running", "name": "manual", "labels": {}},
                ]
            else:
                raise AssertionError(f"unexpected smoke endpoint {request.url.path}")
            return httpx.Response(
                200,
                json={
                    key: values,
                    "meta": {
                        "pagination": {
                            "page": 1,
                            "next_page": None,
                            "last_page": 1,
                            "total_entries": len(values),
                        }
                    },
                },
            )

        client = httpx.AsyncClient
        monkeypatch.setattr(
            "cloud_platform.providers.hetzner.client.httpx.AsyncClient",
            lambda **kwargs: client(**kwargs, transport=httpx.MockTransport(handle)),
        )
        return requests

    @staticmethod
    def _settings() -> Settings:
        return Settings(
            _env_file=None,
            hetzner_api_token="",
            hetzner_accounts=[
                HetznerAccountSettings(id="main", api_token="main-secret", server_limit=5),
                HetznerAccountSettings(id="old", api_token="old-secret", state="draining"),
                HetznerAccountSettings(id="disabled", api_token="", state="disabled"),
            ],
        )

    def test_explicit_empty_accounts_skip_without_using_legacy_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requests = self._transport(monkeypatch)
        settings = Settings(
            _env_file=None,
            hetzner_api_token="unused-secret",
            hetzner_accounts=[],
        )

        result = smoke.check_provider_read_only("hetzner", None, settings)

        assert result.ok is True
        assert "skipped" in result.detail
        assert not requests

    def test_account_only_configuration_reads_each_managed_project(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requests = self._transport(monkeypatch)

        result = smoke.check_provider_read_only("hetzner", "https://api.test/v1", self._settings())

        assert result.ok is True
        assert {request.headers["authorization"] for request in requests} == {
            "Bearer main-secret",
            "Bearer old-secret",
        }
        assert all(request.url.host == "api.test" for request in requests)
        assert "account old (draining)" in result.detail
        assert "Project servers=2; operator-configured server ceiling=5" in result.detail
        assert "operator-configured server ceiling=unknown" in result.detail
        assert "disabled" not in result.detail
        assert "secret" not in result.detail

    def test_provider_failure_is_unknown_and_does_not_expose_payload(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requests = self._transport(monkeypatch, fail=True)

        result = smoke.check_provider_read_only("hetzner", "https://api.test", self._settings())

        assert result.ok is False
        assert "Project availability unknown" in result.detail
        assert "private-token-diagnostic" not in result.detail
        assert "secret" not in result.detail
        assert {request.headers["authorization"] for request in requests} == {
            "Bearer main-secret",
            "Bearer old-secret",
        }


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


def test_main_provider_check_accepts_account_only_settings(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    requests = TestProviderCheck._transport(monkeypatch)
    monkeypatch.setattr(
        "cloud_platform.core.config.get_settings",
        TestProviderCheck._settings,
    )
    monkeypatch.setattr(smoke, "http_get", _fake_http(OK))

    code = smoke.main(
        [
            "--base-url",
            "http://x",
            "--provider-key",
            "hetzner",
            "--provider-base-url",
            "https://api.test/v1",
        ]
    )

    assert code == 0
    assert requests
    text = capsys.readouterr().out
    assert "Project servers=2" in text
    assert "skipped" not in text
    assert "secret" not in text
