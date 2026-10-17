"""Tests for the post-deploy smoke test (M12-007).

Acceptance: health + safe read-only provider checks.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

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
    "http://x/health/ready": (200, json.dumps({"status": "ok"})),
    "http://x/metrics": (200, "cloud_platform_api_requests_total 1\n"),
}


@pytest.fixture(autouse=True)
def isolated_runtime_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    # HTTP mocks must not silently stand in for real DB/worker evidence.
    monkeypatch.setattr(smoke, "database_revisions", smoke.repo_head_revision)
    monkeypatch.setattr(smoke, "worker_health", lambda: 0)
    monkeypatch.setattr(smoke, "financial_readiness", AsyncMock(return_value=(0, 0, 0, 0)))


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

    @pytest.mark.parametrize("body", ["[]", "null", '"ok"', "42", "not-json"])
    def test_non_object_health_body_fails_without_crashing(self, body: str) -> None:
        with patch.object(smoke, "http_get", return_value=(200, body)):
            assert all(not result.ok for result in smoke.check_health("http://x"))

    def test_worker_check_uses_arq_not_api(self) -> None:
        with patch.object(smoke, "http_get", side_effect=AssertionError("not worker evidence")):
            assert smoke.check_worker("http://x").ok is True

    def test_api_green_does_not_hide_dead_worker(self) -> None:
        with patch.object(smoke, "worker_health", return_value=1):
            assert smoke.check_worker("http://x").ok is False

    def test_worker_failure_is_redacted(self) -> None:
        with patch.object(smoke, "worker_health", side_effect=RuntimeError("secret-value")):
            result = smoke.check_worker("http://x")
        assert result.ok is False
        assert "secret-value" not in result.detail


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
        assert result.ok is False

    def test_multiple_observed_heads_fail(self) -> None:
        with patch.object(smoke, "database_revisions", return_value="0050,0053"):
            assert smoke.check_migration_head("http://x", "0053").ok is False

    def test_database_probe_failure_is_not_a_green_expected_head(self) -> None:
        with patch.object(smoke, "database_revisions", side_effect=RuntimeError("secret-dsn")):
            result = smoke.check_migration_head("http://x", "0053")
        assert not result.ok
        assert "secret-dsn" not in result.detail


class TestFinancialReadOnly:
    @pytest.mark.parametrize("balance, expected_errors", [(500, 0), (499, 1)])
    def test_real_repositories_reconcile_one_shared_snapshot_without_writes(
        self, balance: int, expected_errors: int
    ) -> None:
        from sqlalchemy import JSON, MetaData, create_engine, event, insert, select
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy.orm import Session

        from cloud_platform.db.base import (
            Hold,
            LedgerEntry,
            PaymentSession,
            User,
            Wallet,
            WalletCurrencyMigration,
        )

        # Exercise real ORM reads and domain reconciliation against persisted
        # facts. Only PostgreSQL DDL types/defaults are adapted for SQLite.
        metadata = MetaData()
        for model in (User, Wallet, Hold, LedgerEntry, WalletCurrencyMigration, PaymentSession):
            table = model.__table__.to_metadata(metadata)
            for column in table.c:
                column.server_default = None
                if isinstance(column.type, JSONB):
                    column.type = JSON()
        engine = create_engine("sqlite://")
        metadata.create_all(engine)
        wallet_id, user_id = uuid4(), uuid4()
        with engine.begin() as connection:
            connection.execute(
                insert(metadata.tables["wallets"]),
                {"id": wallet_id, "user_id": user_id, "currency": "IRT", "balance": balance},
            )
            connection.execute(
                insert(metadata.tables["ledger"]),
                {
                    "id": uuid4(),
                    "wallet_id": wallet_id,
                    "entry_type": "deposit",
                    "currency": "IRT",
                    "amount": 500,
                    "idempotency_key": "deposit-private-payment-reference",
                    "created_at": datetime(2026, 1, 1),
                },
            )
            connection.execute(
                insert(metadata.tables["payment_sessions"]),
                {
                    "id": uuid4(),
                    "user_id": user_id,
                    "gateway_key": "test",
                    "amount_minor": 500,
                    "currency": "IRT",
                    "status": "pending",
                    "idempotency_key": "private-payment-reference",
                },
            )

        statements: list[str] = []

        def record(
            connection: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            with (
                engine.connect() as connection,
                connection.begin(),
                Session(bind=connection) as session,
            ):
                connection.exec_driver_sql("PRAGMA query_only = ON")
                statements.clear()

                class AsyncReadSession:
                    async def execute(self, statement: Any) -> Any:
                        return session.execute(statement)

                observed = asyncio.run(
                    smoke._financial_snapshot(cast(AsyncSession, AsyncReadSession()))
                )
                assert observed == (1, expected_errors, 0, 1)
                assert session.in_transaction()
                assert connection.in_transaction()
                assert statements and all(
                    sql.lstrip().upper().startswith("SELECT ") for sql in statements
                )
                assert (
                    connection.execute(select(metadata.tables["wallets"].c.balance)).scalar_one()
                    == balance
                )
        finally:
            engine.dispose()

    def test_financial_error_count_fails_without_exposing_customer_data(self) -> None:
        with patch.object(smoke, "financial_readiness", AsyncMock(return_value=(1, 1, 0, 1))):
            result = smoke.check_financial_read_only()
        assert not result.ok
        assert result.detail == "wallets=1, ledger errors=1, warnings=0, payment sessions=1"

    def test_financial_probe_exception_is_redacted(self) -> None:
        with patch.object(
            smoke, "financial_readiness", AsyncMock(side_effect=RuntimeError("secret-database-url"))
        ):
            result = smoke.check_financial_read_only()
        assert not result.ok
        assert "RuntimeError" in result.detail
        assert "secret" not in result.detail


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
