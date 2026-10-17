"""Tests for dependency readiness checks (core.health)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from cloud_platform.api.routes import health as http_health
from cloud_platform.core.container import get_container
from cloud_platform.core.health import (
    CheckResult,
    PostgresReadiness,
    ReadinessStatus,
    RedisReadiness,
    check_readiness,
)


class _FakeSession:
    def __init__(self, execute_error: Exception | None = None) -> None:
        self._execute_error = execute_error
        self.executed: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, stmt: Any) -> None:
        self.executed.append(str(stmt))
        if self._execute_error is not None:
            raise self._execute_error


class _FakeSessionFactory:
    def __init__(
        self,
        execute_error: Exception | None = None,
        enter_error: Exception | None = None,
    ) -> None:
        self._execute_error = execute_error
        self._enter_error = enter_error
        self.last_session: _FakeSession | None = None

    def __call__(self) -> _FakeSession:
        if self._enter_error is not None:
            raise self._enter_error
        session = _FakeSession(execute_error=self._execute_error)
        self.last_session = session
        return session


class _FakeRedis:
    def __init__(self, ping_error: Exception | None = None, pong: object = True) -> None:
        self._ping_error = ping_error
        self._pong = pong
        self.ping_calls = 0

    async def ping(self) -> object:
        self.ping_calls += 1
        if self._ping_error is not None:
            raise self._ping_error
        return self._pong


class TestPostgresReadiness:
    async def test_ready_on_select_one(self) -> None:
        factory = _FakeSessionFactory()
        result = await PostgresReadiness(factory).check()  # type: ignore[arg-type]
        assert result.name == "postgres"
        assert result.status is ReadinessStatus.READY
        assert result.latency_ms >= 0.0
        assert factory.last_session is not None
        assert len(factory.last_session.executed) == 1
        assert "SELECT 1" in factory.last_session.executed[0]

    async def test_unready_when_session_enter_raises(self) -> None:
        factory = _FakeSessionFactory(enter_error=RuntimeError("no pool"))
        result = await PostgresReadiness(factory).check()  # type: ignore[arg-type]
        assert result.status is ReadinessStatus.UNREADY
        assert result.detail.startswith("RuntimeError")

    async def test_unready_when_execute_raises(self) -> None:
        factory = _FakeSessionFactory(execute_error=RuntimeError("connection reset"))
        result = await PostgresReadiness(factory).check()  # type: ignore[arg-type]
        assert result.status is ReadinessStatus.UNREADY
        assert "RuntimeError" in result.detail
        assert "connection reset" in result.detail

    async def test_detail_truncated_to_200_chars(self) -> None:
        factory = _FakeSessionFactory(execute_error=RuntimeError("x" * 500))
        result = await PostgresReadiness(factory).check()  # type: ignore[arg-type]
        assert len(result.detail) <= 200
        assert result.detail.startswith("RuntimeError")


class TestRedisReadiness:
    async def test_ready_on_ping(self) -> None:
        client = _FakeRedis()
        result = await RedisReadiness(client).check()
        assert result.name == "redis"
        assert result.status is ReadinessStatus.READY
        assert client.ping_calls == 1

    async def test_unready_when_ping_raises(self) -> None:
        client = _FakeRedis(ping_error=ConnectionError("refused"))
        result = await RedisReadiness(client).check()
        assert result.status is ReadinessStatus.UNREADY
        assert "ConnectionError" in result.detail


class _StaticCheck:
    def __init__(self, name: str, status: ReadinessStatus) -> None:
        self._name = name
        self._status = status

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> CheckResult:
        return CheckResult(
            name=self._name,
            status=self._status,
            latency_ms=1.0,
            detail="" if self._status is ReadinessStatus.READY else "boom",
        )


class TestCheckReadiness:
    async def test_all_ready_report(self) -> None:
        report = await check_readiness(
            [
                _StaticCheck("postgres", ReadinessStatus.READY),
                _StaticCheck("redis", ReadinessStatus.READY),
            ]
        )
        assert report.ready is True
        assert report.status is ReadinessStatus.READY
        assert [r.name for r in report.results] == ["postgres", "redis"]

    async def test_single_failure_makes_report_unready(self) -> None:
        report = await check_readiness(
            [
                _StaticCheck("postgres", ReadinessStatus.READY),
                _StaticCheck("redis", ReadinessStatus.UNREADY),
            ]
        )
        assert report.ready is False
        assert report.status is ReadinessStatus.UNREADY

    async def test_results_ordered_as_inputs(self) -> None:
        checks = [_StaticCheck(n, ReadinessStatus.READY) for n in ("a", "b", "c", "d")]
        report = await check_readiness(checks)
        assert [r.name for r in report.results] == ["a", "b", "c", "d"]

    async def test_as_dict_is_json_safe_shape(self) -> None:
        report = await check_readiness(
            [
                _StaticCheck("postgres", ReadinessStatus.READY),
                _StaticCheck("redis", ReadinessStatus.UNREADY),
            ]
        )
        payload = report.as_dict()
        assert payload["status"] == "unready"
        checks = payload["checks"]
        assert isinstance(checks, list)
        first = checks[0]
        for key in ("name", "status", "latency_ms", "detail"):
            assert key in first


def _http_health_app(factory: _FakeSessionFactory, redis: _FakeRedis) -> FastAPI:
    app = FastAPI()
    app.include_router(http_health.router)
    app.dependency_overrides[get_container] = lambda: SimpleNamespace(
        session_factory=factory, session_store=lambda: redis
    )
    return app


@pytest.mark.parametrize(
    "database_fails,redis_fails,status",
    [(False, False, 200), (True, False, 503), (False, True, 503)],
)
async def test_http_readiness_gates_activation_without_exposing_dependency_errors(
    database_fails: bool, redis_fails: bool, status: int
) -> None:
    private_detail = "dependency-error-must-not-be-public"
    app = _http_health_app(
        _FakeSessionFactory(execute_error=RuntimeError(private_detail) if database_fails else None),
        _FakeRedis(ping_error=ConnectionError(private_detail) if redis_fails else None),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://health.test"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == status
    assert response.json()["status"] == ("ok" if status == 200 else "unready")
    assert {check["name"]: check["status"] for check in response.json()["checks"]} == {
        "postgres": "unready" if database_fails else "ready",
        "redis": "unready" if redis_fails else "ready",
    }
    assert private_detail not in response.text


async def test_stalled_dependency_has_bounded_unready_response(monkeypatch) -> None:
    class StalledRedis(_FakeRedis):
        async def ping(self) -> object:
            await asyncio.Event().wait()

    monkeypatch.setattr(http_health, "READINESS_TIMEOUT_SECONDS", 0.01)
    app = _http_health_app(_FakeSessionFactory(), StalledRedis())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://health.test"
    ) as client:
        response = await asyncio.wait_for(client.get("/health/ready"), timeout=1)
    assert response.status_code == 503
    assert response.json()["status"] == "unready"


async def test_liveness_does_not_require_container_or_dependencies() -> None:
    app = _http_health_app(_FakeSessionFactory(), _FakeRedis())

    def unavailable_container() -> None:
        raise RuntimeError("database and session store are down")

    app.dependency_overrides[get_container] = unavailable_container
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://health.test"
    ) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
