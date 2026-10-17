"""A live process or stale Redis sentinel is not proof that ARQ is progressing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cloud_platform.worker import health


@pytest.fixture
def process(monkeypatch):
    def started(pid: int) -> str:
        if pid != 42:
            raise FileNotFoundError("process no longer exists")
        return "12345"

    monkeypatch.setattr(health, "_process_started", started)


@pytest.mark.parametrize("age, expected", [(0, True), (45, True), (45.001, False), (-0.001, False)])
def test_probe_enforces_actual_progress_window(tmp_path: Path, process, age, expected):
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps({"pid": 42, "process_started": "12345", "observed_at": 1000}))
    assert health.worker_healthy(path, now=1000 + age) is expected


@pytest.mark.parametrize(
    "changes",
    [
        {"pid": True},
        {"pid": 999},
        {"observed_at": True},
        {"observed_at": float("nan")},
        {"process_started": "previous-container"},
    ],
)
def test_probe_rejects_dead_reused_or_malformed_process_evidence(tmp_path: Path, process, changes):
    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({"pid": 42, "process_started": "12345", "observed_at": 1000} | changes)
    )
    assert health.worker_healthy(path, now=1001) is False


async def test_observer_does_not_freshen_a_stale_redis_sentinel(tmp_path: Path, monkeypatch):
    path = tmp_path / "heartbeat.json"
    values = iter([b"old-worker", b"old-worker", b"current-worker-progress"])
    observations = []

    class Redis:
        default_queue_name = "fixture-queue"

        async def get(self, key):
            return next(values)

    async def tick(_seconds):
        observations.append(json.loads(path.read_text()) if path.exists() else None)
        if len(observations) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(health, "_process_started", lambda _pid: "current-process")
    monkeypatch.setattr(health.asyncio, "sleep", tick)
    with pytest.raises(asyncio.CancelledError):
        await health.observe_heartbeat(Redis(), path)
    assert observations[:2] == [None, None]
    assert observations[2]["process_started"] == "current-process"
    assert health.worker_healthy(path) is True
