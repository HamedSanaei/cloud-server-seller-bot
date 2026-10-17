"""Stdlib-only probe of progress observed from ARQ's real Redis heartbeat."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

HEARTBEAT_PATH = Path(tempfile.gettempdir()) / "cloud-platform-worker-heartbeat.json"
# Six real 30s ARQ heartbeat periods / three 60s Docker probe intervals.
# Bounded CPU-starvation tolerance; a dead worker/Redis still becomes stale.
MAX_HEARTBEAT_AGE_SECONDS = 180


def _process_started(pid: int) -> str:
    # PID 1 is reused after a container restart, but Linux start ticks are not.
    return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]


def worker_healthy(path: Path = HEARTBEAT_PATH, *, now: float | None = None) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        pid, observed = record["pid"], record["observed_at"]
        if type(pid) is not int or pid <= 0 or type(observed) not in (int, float):
            return False
        age = (time.monotonic() if now is None else now) - observed
        if not math.isfinite(age) or not 0 <= age <= MAX_HEARTBEAT_AGE_SECONDS:
            return False
        if record["process_started"] != _process_started(pid):
            return False
    except (OSError, ValueError, KeyError, TypeError, OverflowError, IndexError):
        return False
    return True


async def observe_heartbeat(redis: Any, path: Path = HEARTBEAT_PATH) -> None:
    # Imported only by the existing worker, never by the healthcheck subprocess.
    import asyncio
    import logging

    from arq.constants import default_queue_name, health_check_key_suffix

    key = (getattr(redis, "default_queue_name", "") or default_queue_name) + health_check_key_suffix
    previous = None
    baseline = False
    process_started = _process_started(os.getpid())
    while True:
        try:
            current = await redis.get(key)
            if baseline and current and current != previous:
                temporary = path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "process_started": process_started,
                            "observed_at": time.monotonic(),
                        }
                    ),
                    encoding="utf-8",
                )
                temporary.replace(path)
            previous = current
            baseline = True
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Worker heartbeat observation unavailable (%s)", type(exc).__name__
            )
        await asyncio.sleep(5)


if __name__ == "__main__":
    healthy = worker_healthy()
    print("Worker heartbeat healthy" if healthy else "Worker heartbeat unavailable/stale")
    raise SystemExit(0 if healthy else 1)
