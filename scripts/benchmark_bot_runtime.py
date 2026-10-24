"""Fresh-process synthetic aiogram polling benchmark; never imports bot routes/settings.

Run each arm in a NEW process, for example:
    uv run python scripts/benchmark_bot_runtime.py --mode baseline --scenario idle
    uv run python scripts/benchmark_bot_runtime.py --mode corrected --scenario burst --seconds 10

This measures actual SDK scheduling, not the production bot's handlers. The only
transport is SyntheticSession: no sockets, database, Redis, credentials or psutil.
Imports/setup are outside the measured window. Each invocation prints one JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import platform
import sys
import time
from array import array
from collections.abc import AsyncGenerator
from pathlib import Path, PurePosixPath
from typing import Any

import aiogram
import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, GetUpdates, TelegramMethod
from aiogram.types import Message, Update, User

from cloud_platform.bot.diagnostics import rss_bytes
from cloud_platform.bot.runtime import BoundedDispatcher

_BATCH_SIZE = 100
_RELEASE_INTERVAL = 0.1
_SAMPLE_INTERVAL = 0.01
_DRAIN_TIMEOUT = 10.0
# Fixed-size histogram: p95 is the containing bucket's upper bound, not interpolation.
_LAG_BOUNDS = (
    0.0,
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.002,
    0.005,
    0.01,
    0.02,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    30.0,
    60.0,
)
_CPU_PAYLOAD = {"synthetic": True, "values": list(range(32)), "label": "fixed-workload"}


def percentile95(values: array) -> float | None:
    observed = sorted(value for value in values if value >= 0)
    return observed[math.ceil(len(observed) * 0.95) - 1] if observed else None


class Sampler:
    """Sample process RSS/task count and scheduled-wakeup lag without retaining tasks."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = interval_seconds
        self.rss_current: int | None = None
        self.rss_peak: int | None = None
        self.tasks_peak = 0
        self.lag_max = 0.0
        self.lag_count = 0
        self.lag_buckets = [0] * (len(_LAG_BOUNDS) + 1)

    def snapshot(self) -> None:
        self.rss_current = rss_bytes()
        if self.rss_current is not None:
            self.rss_peak = max(self.rss_peak or 0, self.rss_current)
        self.tasks_peak = max(self.tasks_peak, len(asyncio.all_tasks()))

    async def run(self) -> None:
        self.snapshot()
        while True:
            # Reset the target each tick: no catch-up busy loop after a long stall.
            target = time.monotonic() + self.interval_seconds
            await asyncio.sleep(self.interval_seconds)
            lag = max(0.0, time.monotonic() - target)
            self.lag_max = max(self.lag_max, lag)
            self.lag_count += 1
            for index, upper in enumerate(_LAG_BOUNDS):
                if lag <= upper:
                    self.lag_buckets[index] += 1
                    break
            else:
                self.lag_buckets[-1] += 1
            self.snapshot()

    def p95(self) -> float | None:
        if not self.lag_count:
            return None
        rank = math.ceil(self.lag_count * 0.95)
        cumulative = 0
        for index, count in enumerate(self.lag_buckets):
            cumulative += count
            if cumulative >= rank:
                return _LAG_BOUNDS[index] if index < len(_LAG_BOUNDS) else self.lag_max
        return None


class SyntheticSession(BaseSession):
    """Telegram offset acknowledgment semantics over deterministic synthetic updates.

    Delivery does NOT acknowledge an update: only a subsequent GetUpdates offset
    greater than its ID does. A cancelled final request still acknowledges its
    offset, just as an accepted request would. Unacknowledged IDs remain eligible
    for redelivery. No list of completed updates/tasks is retained.
    """

    def __init__(self, scenario: str, count: int) -> None:
        super().__init__()
        self.scenario = scenario
        self.count = 0 if scenario == "idle" else count
        self.started = 0.0
        self.poll_requests = 0
        self.poll_responses = 0
        self.empty_responses = 0
        self.get_me_requests = 0
        self.received = 0
        self.received_unique = 0
        self.highest_returned = 0
        self.acknowledged = 0
        self.last_offset = 0
        self.deliveries = array("I", [0]) * self.count
        self.closed = False

    def available(self) -> int:
        if self.scenario != "sustained":
            return self.count
        batches = int(max(0.0, time.monotonic() - self.started) / _RELEASE_INTERVAL) + 1
        return min(self.count, batches * _BATCH_SIZE)

    def released_at(self, update_id: int) -> float:
        batch = (update_id - 1) // _BATCH_SIZE
        return self.started + (batch * _RELEASE_INTERVAL if self.scenario == "sustained" else 0)

    async def close(self) -> None:
        self.closed = True

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - SDK interface
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise RuntimeError("network and file streaming are forbidden in this benchmark")
        yield b""  # Required async-generator interface; this branch is unreachable.

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109
    ) -> Any:
        if self.closed:
            raise RuntimeError("request after synthetic session close")
        if isinstance(method, GetMe):
            self.get_me_requests += 1
            return User(id=1, is_bot=True, first_name="synthetic", username="synthetic_bot")
        if not isinstance(method, GetUpdates):
            raise RuntimeError("only synthetic GetMe/GetUpdates are allowed")
        self.poll_requests += 1
        offset = method.offset or 0
        if offset < self.last_offset or offset > self.highest_returned + 1:
            raise RuntimeError("poller produced an invalid acknowledgment offset")
        self.last_offset = offset
        self.acknowledged = max(self.acknowledged, max(0, offset - 1))
        first = max(1, offset)
        deadline = time.monotonic() + (method.timeout or 0)
        # A zero-time yield models an asynchronous transport boundary; ready
        # batches have no artificial network latency or throughput throttling.
        await asyncio.sleep(0)
        while True:
            available = self.available()
            if first <= available:
                last = min(available, first + min(method.limit or _BATCH_SIZE, _BATCH_SIZE) - 1)
                updates = []
                for update_id in range(first, last + 1):
                    updates.append(
                        Update.model_validate(
                            {
                                "update_id": update_id,
                                "message": {
                                    "message_id": update_id,
                                    "date": 1577836800,
                                    "chat": {"id": 1, "type": "private"},
                                    "text": "synthetic",
                                },
                            },
                            context={"bot": bot},
                        )
                    )
                    index = update_id - 1
                    self.received_unique += self.deliveries[index] == 0
                    self.deliveries[index] += 1
                self.highest_returned = max(self.highest_returned, last)
                self.received += len(updates)
                self.poll_responses += 1
                return updates
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.poll_responses += 1
                self.empty_responses += 1
                return []
            wait = remaining
            if self.scenario == "sustained" and available < self.count:
                next_release = self.started + (available // _BATCH_SIZE) * _RELEASE_INTERVAL
                wait = min(wait, max(0.0, next_release - time.monotonic()))
            await asyncio.sleep(wait)


def cgroup_snapshot() -> dict[str, Any] | None:
    """Read cgroup v2 membership, with a root-bind fallback for isolated OCI mounts."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        membership = next(
            line.split("::", 1)[1]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        candidates: list[Path] = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            if after.split()[0] != "cgroup2":
                continue
            fields = before.split()
            root, mount = PurePosixPath(fields[3]), Path(fields[4])
            try:
                relative = PurePosixPath(membership).relative_to(root)
            except ValueError:
                pass
            else:
                # Kernel paths can contain parent components outside a cgroup
                # namespace; do not traverse outside the mounted controller.
                if ".." not in relative.parts:
                    candidates.append(mount / str(relative))
            candidates.append(mount)
        for directory in candidates:
            try:
                cpu_max = (directory / "cpu.max").read_text().strip()
                memory_max = (directory / "memory.max").read_text().strip()
                stat = {}
                for line in (directory / "cpu.stat").read_text().splitlines():
                    key, value = line.split()
                    stat[key] = int(value)
                return {
                    "version": 2,
                    "cpu.max": cpu_max,
                    "memory.max": memory_max,
                    "cpu_stat": stat,
                }
            except (OSError, ValueError):
                continue
    except (OSError, ValueError, StopIteration):
        pass
    return {"version": None, "cpu.max": None, "memory.max": None, "cpu_stat": {}}


def pending_handlers(dispatcher: Dispatcher) -> tuple[asyncio.Task[Any], ...]:
    return tuple(task for task in dispatcher._handle_update_tasks if not task.done())


async def run(args: argparse.Namespace) -> dict[str, Any]:
    dispatcher = (Dispatcher if args.mode == "baseline" else BoundedDispatcher)(disable_fsm=True)
    session = SyntheticSession(args.scenario, args.updates)
    # Deliberately fixed fake token; the synthetic session cannot issue HTTP.
    bot = Bot(token="1:synthetic-benchmark-only", session=session)
    processed = array("I", [0]) * session.count
    durations = array("d", [-1.0]) * session.count
    queue_latencies = array("d", [-1.0]) * session.count
    handled = 0
    started_handlers = 0
    duplicate_processing = 0
    cancelled_handlers = 0
    handler_errors = 0

    async def handler(message: Message) -> None:
        nonlocal handled, started_handlers, duplicate_processing, cancelled_handlers, handler_errors
        started = time.monotonic()
        index = message.message_id - 1
        started_handlers += 1
        duplicate_processing += processed[index] > 0
        processed[index] += 1
        try:
            # Identical fixed serialization work plus simulated asynchronous I/O
            # in both arms. No real production bot route is registered or called.
            for _ in range(8):
                json.dumps(_CPU_PAYLOAD, sort_keys=True, separators=(",", ":"))
            await asyncio.sleep(args.handler_delay)
        except asyncio.CancelledError:
            cancelled_handlers += 1
            raise
        except Exception:
            handler_errors += 1
            raise
        else:
            finished = time.monotonic()
            handled += 1
            # Exactly one slot per generated ID: duplicate processing cannot
            # cause unbounded measurement storage.
            if durations[index] < 0:
                durations[index] = finished - started
                queue_latencies[index] = finished - session.released_at(message.message_id)

    dispatcher.message.register(handler)
    sampler = Sampler(args.sample_interval)
    polling_timeout = 10 if args.mode == "baseline" else 30
    concurrency = None if args.mode == "baseline" else 16
    before_cgroup = cgroup_snapshot()
    start_cpu, start_wall = time.process_time(), time.monotonic()
    session.started = start_wall
    sample_task = asyncio.create_task(sampler.run(), name="benchmark-sampler")
    options: dict[str, Any] = {"handle_signals": False, "close_bot_session": False}
    if args.mode == "corrected":
        options.update(polling_timeout=polling_timeout, tasks_concurrency_limit=concurrency)
    # Baseline deliberately exercises start_polling's actual SDK defaults.
    polling_task = asyncio.create_task(
        dispatcher.start_polling(bot, **options), name="benchmark-polling"
    )
    deadline_task = asyncio.create_task(asyncio.sleep(args.seconds), name="benchmark-deadline")
    outstanding_at_stop = 0
    try:
        done, _ = await asyncio.wait(
            (polling_task, deadline_task), return_when=asyncio.FIRST_COMPLETED
        )
        if polling_task in done:
            await polling_task
            raise RuntimeError("poller exited before the benchmark deadline")
        # SDK stop cancels and awaits its owned _polling task, including a
        # pending GetUpdates or semaphore acquisition, without closing the bot.
        await dispatcher.stop_polling()
        await polling_task
        outstanding_at_stop = len(pending_handlers(dispatcher))
        handled_at_stop = handled
        started_at_stop = started_handlers
        generated_at_stop = session.available()
        sampler.snapshot()
        wall_seconds = time.monotonic() - start_wall
        cpu_seconds = time.process_time() - start_cpu
        after_cgroup = cgroup_snapshot()
    finally:
        deadline_task.cancel()
        if not polling_task.done():
            # Also clean up SDK-owned pollers on an exceptional harness exit;
            # cancelling start_polling directly would leave its children alive.
            if dispatcher._running_lock.locked():
                await dispatcher.stop_polling()
            await asyncio.gather(polling_task, return_exceptions=True)
        sample_task.cancel()
        await asyncio.gather(deadline_task, sample_task, return_exceptions=True)
        drain_wall, drain_cpu = time.monotonic(), time.process_time()
        if isinstance(dispatcher, BoundedDispatcher):
            await dispatcher.drain_handlers(_DRAIN_TIMEOUT)
        else:
            # Baseline has no owned drain API. Observe its outstanding work
            # before this harness-only cleanup; never leak tasks into loop close.
            owned = pending_handlers(dispatcher)
            if owned:
                _, pending = await asyncio.wait(owned, timeout=_DRAIN_TIMEOUT)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*owned, return_exceptions=True)
        shutdown_wall = time.monotonic() - drain_wall
        shutdown_cpu = time.process_time() - drain_cpu
        await bot.session.close()
        await dispatcher.storage.close()
    throttle_delta = None
    if before_cgroup is not None and after_cgroup is not None:
        before_stat, after_stat = before_cgroup["cpu_stat"], after_cgroup["cpu_stat"]
        throttle_delta = {
            key: after_stat[key] - before_stat[key]
            for key in ("nr_periods", "nr_throttled", "throttled_usec")
            if key in before_stat and key in after_stat
        }
    return {
        "mode": args.mode,
        "scenario": args.scenario,
        "environment": {
            "aiogram": aiogram.__version__,
            "python": platform.python_version(),
            "platform": sys.platform,
            "machine": platform.machine(),
            "cgroup": after_cgroup,
            "cgroup_throttling_delta": throttle_delta,
        },
        "workload": {
            "kind": "synthetic_not_real_bot_routes",
            "requested_seconds": args.seconds,
            "update_limit": args.updates,
            "batch_size": _BATCH_SIZE,
            "sustained_batch_interval_seconds": _RELEASE_INTERVAL,
            "handler_delay_seconds": args.handler_delay,
            "json_serializations_per_handler": 8,
            "json_payload": _CPU_PAYLOAD,
            "fsm_disabled": True,
            "polling_timeout_seconds": polling_timeout,
            "tasks_concurrency_limit": concurrency,
        },
        "measurement": {
            "window": "polling_start_through_poller_stop_excludes_imports_setup_and_handler_drain",
            "cpu_percent_one_core": 100.0 * cpu_seconds / wall_seconds,
            "cpu_seconds": cpu_seconds,
            "wall_seconds": wall_seconds,
            "rss_current_bytes_at_stop": sampler.rss_current,
            "rss_peak_observed_bytes": sampler.rss_peak,
            "rss_current_bytes_after_shutdown": rss_bytes(),
            "sampler_interval_seconds": args.sample_interval,
            "active_asyncio_tasks_peak_observed_including_sampler": sampler.tasks_peak,
            "loop_lag_samples": sampler.lag_count,
            "loop_lag_max_seconds": sampler.lag_max,
            "loop_lag_p95_upper_seconds": sampler.p95(),
            "loop_lag_p95_method": "fixed_histogram_upper_bound",
            "handler_execution_p95_seconds": percentile95(durations),
            "end_to_end_release_to_completion_p95_seconds": percentile95(queue_latencies),
            "handler_percentile_method": "nearest_rank_first_completion_per_id_includes_drain",
            "poll_requests_per_minute": session.poll_requests * 60.0 / wall_seconds,
        },
        "counts": {
            "generated_at_stop": generated_at_stop,
            "received": session.received,
            "received_unique": session.received_unique,
            "duplicate_deliveries": session.received - session.received_unique,
            "acknowledged_by_offset": session.acknowledged,
            "last_request_offset": session.last_offset,
            "unacknowledged_delivered_unique": session.received_unique - session.acknowledged,
            "handler_started_at_stop": started_at_stop,
            "handled_at_stop": handled_at_stop,
            "handler_started_total": started_handlers,
            "handled_total_including_drain": handled,
            "duplicate_processing": duplicate_processing,
            "handler_errors": handler_errors,
            "handler_cancellations": cancelled_handlers,
            "get_me_requests": session.get_me_requests,
            "poll_requests_started": session.poll_requests,
            "poll_responses_completed": session.poll_responses,
            "empty_poll_responses": session.empty_responses,
            "outstanding_handler_tasks_at_stop_before_cleanup": outstanding_at_stop,
            "remaining_handler_tasks_after_shutdown": len(pending_handlers(dispatcher)),
        },
        "shutdown": {
            "drain_timeout_seconds": _DRAIN_TIMEOUT,
            "drain_wall_seconds": shutdown_wall,
            "drain_cpu_seconds": shutdown_cpu,
            "baseline_cleanup": "harness_owned_wait_then_cancel"
            if args.mode == "baseline"
            else None,
        },
        "semantics": {
            "release": "burst_all_at_start; sustained_100_at_start_then_each_100ms; idle_none",
            "poll_requests": (
                "started_requests_including_final_cancelled_long_poll; offset_ack_on_request"
            ),
            "queue_latency": (
                "scheduled_synthetic_release_to_handler_completion_including_backpressure"
            ),
            "lag": "wakeup_overshoot_of_sampler_sleep; no_catchup; polling_window_only",
            "rss": "current_working_set_or_resident_pages; sampled_peak_not_OS_lifetime_high_water",
            "task_peak": "sampled_live_asyncio_tasks_including_runner_sampler_poller_and_deadline",
            "storage": (
                "per_update_arrays_capped_at_100000; fixed_lag_histogram; no_completed_task_history"
            ),
            "isolation": ("fresh_process; synthetic_BaseSession; no_network_DB_Redis_settings"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("baseline", "corrected"), required=True)
    parser.add_argument("--scenario", choices=("idle", "burst", "sustained"), required=True)
    parser.add_argument(
        "--seconds", type=float, help="defaults: idle=35, load=10; excludes handler drain"
    )
    parser.add_argument("--updates", type=int, default=1000, help="1..100000; ignored for idle")
    parser.add_argument("--handler-delay", type=float, default=0.05)
    parser.add_argument(
        "--sample-interval",
        type=float,
        help="observer interval seconds: defaults idle=0.25, load=0.01",
    )
    args = parser.parse_args()
    if args.sample_interval is None:
        # High-frequency observation itself costs CPU; idle needs only 4Hz.
        args.sample_interval = 0.25 if args.scenario == "idle" else _SAMPLE_INTERVAL
    if not math.isfinite(args.sample_interval) or args.sample_interval <= 0:
        parser.error("--sample-interval must be finite and positive")
    if args.seconds is None:
        args.seconds = 35.0 if args.scenario == "idle" else 10.0
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("--seconds must be finite and positive")
    if not 1 <= args.updates <= 100000:
        parser.error("--updates must be between 1 and 100000")
    if not math.isfinite(args.handler_delay) or args.handler_delay < 0:
        parser.error("--handler-delay must be finite and nonnegative")
    # Only this standalone benchmark suppresses SDK logs (which include IDs).
    # Production settings and logging setup are never loaded or initialized here.
    logging.disable(logging.CRITICAL)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.CRITICAL))
    with contextlib.suppress(KeyboardInterrupt):
        result = asyncio.run(run(args))
        print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
