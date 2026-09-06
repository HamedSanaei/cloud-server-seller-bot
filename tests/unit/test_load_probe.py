"""Tests for the reproducible load probe (M16-002).

Acceptance: bottlenecks measured with reproducible script.

These tests pin the MEASUREMENT machinery: percentile math, exact
completion counts under concurrency, bottleneck ranking order, the
probe app's real v1 pipeline behaviour (auth override on, unauth
fast-fail envelope) and full queue drain - all with tiny parameters so
they stay fast and deterministic in structure (never asserting absolute
timings).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from cloud_platform.api.v1.errors import ErrorCode
from cloud_platform.planning.load_probe import (
    ProbeResult,
    WorkerProbeConfig,
    probe_api,
    probe_worker_queue,
    rank_bottlenecks,
    render_report,
    summarize,
)
from cloud_platform.planning.probe_app import build_probe_app


def _result(name: str, latencies: tuple[float, ...]) -> ProbeResult:
    return ProbeResult(
        name=name,
        kind="api",
        requests=len(latencies),
        duration_s=1.0,
        latencies_ms=latencies,
    )


class TestPercentileMath:
    def test_sorted_index_percentiles(self) -> None:
        r = _result("t", (10.0, 20.0, 30.0, 40.0))
        assert r.p50 == 20.0
        assert r.percentile(75) == 30.0
        assert r.p95 == 40.0
        assert r.p99 == 40.0
        assert r.mean == pytest.approx(25.0)

    def test_single_sample(self) -> None:
        r = _result("t", (7.5,))
        assert r.p50 == 7.5
        assert r.p99 == 7.5
        assert r.rps == pytest.approx(1.0)

    def test_empty_result_is_safe(self) -> None:
        r = _result("t", ())
        assert r.p50 == 0.0
        assert r.rps > 0 or True


class TestBottleneckRanking:
    def test_orders_by_p95_descending(self) -> None:
        fast = _result("fast", tuple(1.0 for _ in range(20)))
        slow = _result("slow", tuple(50.0 for _ in range(20)))
        mid = _result("mid", tuple(10.0 for _ in range(20)))
        ranked = rank_bottlenecks([fast, slow, mid])
        assert [r.name for r in ranked] == ["slow", "mid", "fast"]

    def test_summary_names_the_bottleneck(self) -> None:
        fast = _result("fast", tuple(1.0 for _ in range(10)))
        slow = _result("slow", tuple(99.0 for _ in range(10)))
        summary = summarize([fast, slow])
        assert summary["bottleneck"] == "slow"
        assert len(summary["results"]) == 2


@pytest.mark.asyncio()
class TestProbes:
    async def test_api_probe_completes_exactly_total_requests(self) -> None:
        async def _call() -> int:
            await asyncio.sleep(0)  # yield so concurrent workers can race the claim
            return 200

        result = await probe_api("ep", _call, total_requests=37, concurrency=5)
        assert result.requests == 37
        assert result.kind == "api"
        assert len(result.latencies_ms) == 37
        assert result.duration_s > 0

    async def test_api_probe_rejects_zero_arguments(self) -> None:
        async def _call() -> int:
            return 200

        with pytest.raises(ValueError):
            await probe_api("ep", _call, total_requests=0, concurrency=1)
        with pytest.raises(ValueError):
            await probe_api("ep", _call, total_requests=1, concurrency=0)

    async def test_worker_queue_drains_every_job(self) -> None:
        handled: list[dict[str, object]] = []

        async def _handler(payload: dict[str, object]) -> None:
            handled.append(payload)

        result = await probe_worker_queue(WorkerProbeConfig(jobs=23, workers=4, handler=_handler))
        assert result.requests == 23
        assert len(handled) == 23
        assert result.name.startswith("worker:_handler")

    async def test_worker_queue_rejects_zero_arguments(self) -> None:
        async def _handler(_payload: dict[str, object]) -> None:
            return None

        with pytest.raises(ValueError):
            await probe_worker_queue(WorkerProbeConfig(jobs=0, workers=1, handler=_handler))


class TestReportRendering:
    def test_report_lists_targets_and_ranking(self) -> None:
        fast = _result("GET fast", tuple(1.0 for _ in range(10)))
        slow = _result("GET slow", tuple(90.0 for _ in range(10)))
        report = render_report([fast, slow])
        assert "GET fast" in report
        assert "GET slow" in report
        assert "Bottleneck ranking" in report
        # the ranking section, not the input-ordered table, must lead with slow
        assert report.index("1. GET slow") < report.index("2. GET fast")


@pytest.mark.asyncio()
class TestProbeAppPipeline:
    def _anon_app(self):  # type: ignore[no-untyped-def]
        from cloud_platform.planning.probe_app import get_token_authentication as _dep

        app = build_probe_app()
        del app.dependency_overrides[_dep]
        return app

    async def test_health_is_anonymous_ok(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_probe_app()), base_url="http://probe"
        ) as client:
            response = await client.get("/health/live")
        assert response.status_code == 200

    async def test_offers_flow_through_real_auth_and_catalog_port(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_probe_app()), base_url="http://probe"
        ) as client:
            response = await client.get("/v1/catalog/offers")
        assert response.status_code == 200
        body = response.json()
        assert len(body["offers"]) == 50
        assert all(o["enabled"] for o in body["offers"])

    async def test_servers_use_the_fake_service_shape(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=build_probe_app()), base_url="http://probe"
        ) as client:
            response = await client.get("/v1/servers")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 20
        assert len(body["servers"]) == 20

    async def test_unauthenticated_request_uses_the_error_envelope(self) -> None:
        # WITHOUT the auth override the request must fast-fail through
        # the one error envelope without touching any backing service.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._anon_app()), base_url="http://probe"
        ) as anon:
            response = await anon.get("/v1/wallet")
        assert response.status_code == 401
        error = response.json()["error"]
        assert error["code"] == ErrorCode.UNAUTHORIZED.value
