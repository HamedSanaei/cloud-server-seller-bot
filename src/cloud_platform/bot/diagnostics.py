"""Opt-in, process-local bot diagnostics without request data or dynamic labels.

Register ``request_middleware`` on the bot session and ``handler_middleware`` as
INNER middleware on each relevant aiogram observer (message/callback_query).
Only attach/start when ``enabled``. ``stop`` cancels the sampler and removes owned
SQLAlchemy listeners and Redis instance wrappers; it never closes their clients.

Snapshots reset interval counters/histograms, but retain in-flight gauges. Duration
buckets are non-cumulative, in seconds; p95_upper_seconds is the upper boundary
containing rank ceil(count * .95), or the observed maximum in the overflow bucket.
It is a conservative bound, not an interpolated percentile. CPU percent measures
process CPU / wall time (100% is one core, and values may exceed 100%). Database
latency covers cursor execution, not pool checkout or whole transactions. Redis
latency covers execute_command, including any client-internal retries. Poll retry
counts mean requests following a failed GetUpdates, not SDK-internal attempts.

Handler timings retain at most 63 static code names plus the ``other`` overflow
bucket for the process lifetime (64 total); every interval drains their counters.
Event-loop lag is the maximum wake drift observed during the reporting interval,
sampled every min(1 second, interval_seconds), not just at the reporting boundary.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import FunctionType, MethodType
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetUpdates, Response, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import TelegramObject
from sqlalchemy import event

from cloud_platform.core.logging import get_logger

_DURATION_BOUNDS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0)
_OUTCOMES = ("success", "error", "cancelled")


class _Duration:
    def __init__(self) -> None:
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0
        self.buckets = [0] * (len(_DURATION_BOUNDS) + 1)

    def observe(self, duration: float) -> None:
        duration = max(0.0, duration)
        self.count += 1
        self.total += duration
        self.maximum = max(self.maximum, duration)
        for index, upper in enumerate(_DURATION_BOUNDS):
            if duration <= upper:
                self.buckets[index] += 1
                break
        else:
            self.buckets[-1] += 1

    def drain(self) -> dict[str, Any]:
        rank = (95 * self.count + 99) // 100
        cumulative = 0
        percentile = None
        if self.count:
            for index, count in enumerate(self.buckets):
                cumulative += count
                if cumulative >= rank:
                    percentile = (
                        _DURATION_BOUNDS[index] if index < len(_DURATION_BOUNDS) else self.maximum
                    )
                    break
        result = {
            "count": self.count,
            "sum_seconds": self.total,
            "max_seconds": self.maximum,
            "p95_upper_seconds": percentile,
            "buckets": self.buckets,
        }
        self.count = 0
        self.total = 0.0
        self.maximum = 0.0
        self.buckets = [0] * (len(_DURATION_BOUNDS) + 1)
        return result


class _Operations:
    def __init__(self) -> None:
        self.active = 0
        self.outcomes = dict.fromkeys(_OUTCOMES, 0)
        self.duration = _Duration()

    def finish(self, start: float, outcome: str, elapsed: float | None = None) -> float:
        if elapsed is None:
            elapsed = max(0.0, time.monotonic() - start)
        self.active -= 1
        self.outcomes[outcome] += 1
        self.duration.observe(elapsed)
        return elapsed

    def drain(self) -> dict[str, Any]:
        outcomes = self.outcomes
        self.outcomes = dict.fromkeys(_OUTCOMES, 0)
        return {
            "active": self.active,
            "completed": sum(outcomes.values()),
            "failed": outcomes["error"],
            **outcomes,
            "duration": self.duration.drain(),
        }


def rss_bytes() -> int | None:
    """Current resident memory, not resource.getrusage's lifetime high-water mark."""
    try:
        if sys.platform.startswith("linux"):
            import os

            with Path("/proc/self/statm").open(encoding="ascii") as source:
                resident_pages = int(source.read(256).split()[1])
            return resident_pages * os.sysconf("SC_PAGE_SIZE")
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class MemoryCounters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t)
                    for name in (
                        "peak_working_set",
                        "working_set",
                        "quota_peak_paged",
                        "quota_paged",
                        "quota_peak_nonpaged",
                        "quota_nonpaged",
                        "pagefile",
                        "peak_pagefile",
                    )
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(MemoryCounters),
                wintypes.DWORD,
            )
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(
                kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                return int(counters.working_set)
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return None


def _handler_name(data: dict[str, Any]) -> str:
    # Never use repr, a data-derived label, or the middleware-wrapped callable.
    callback = getattr(data.get("handler"), "callback", None)
    if isinstance(callback, MethodType):
        callback = callback.__func__
    if isinstance(callback, FunctionType):
        name = callback.__code__.co_name
        if name.isidentifier() and name.isascii() and len(name) <= 80:
            return name
    return "unknown"


class _RequestMiddleware(BaseRequestMiddleware):
    def __init__(self, diagnostics: BotDiagnostics) -> None:
        self.diagnostics = diagnostics

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        owner = self.diagnostics
        if not owner.enabled:
            return await make_request(bot, method)
        polling = isinstance(method, GetUpdates)
        operations = owner._poll if polling else owner._telegram_api
        if polling:
            owner._poll_requests += 1
        if polling and owner._poll_failed:
            owner._poll_retries += 1
        operations.active += 1
        start = time.monotonic()
        outcome = "error"
        try:
            response = await make_request(bot, method)
            if polling:
                if isinstance(response, list):
                    owner._updates += len(response)
                owner._poll_failed = False
            outcome = "success"
            return response
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TelegramRetryAfter:
            if polling:
                owner._retry_after += 1
                owner._poll_failed = True
            raise
        except Exception:
            if polling:
                owner._poll_failed = True
            raise
        finally:
            operations.finish(start, outcome)


class _HandlerMiddleware(BaseMiddleware):
    def __init__(self, diagnostics: BotDiagnostics) -> None:
        self.diagnostics = diagnostics

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        owner = self.diagnostics
        if not owner.enabled:
            return await handler(event, data)
        owner._handlers.active += 1
        name = _handler_name(data)
        if name not in owner._handler_timings and len(owner._handler_timings) < 64:
            owner._handler_timings[name] = _Operations()
        timing = owner._handler_timings.get(name, owner._handler_timings["other"])
        timing.active += 1
        start = time.monotonic()
        outcome = "error"
        try:
            result = await handler(event, data)
            outcome = "success"
            return result
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            elapsed = owner._handlers.finish(start, outcome)
            timing.finish(start, outcome, elapsed)
            if elapsed >= owner.slow_handler_seconds:
                owner._slow_handlers += 1
                # At most one warning per interval, regardless of handler cardinality.
                if not owner._slow_warning_sent:
                    owner._slow_warning_sent = True
                    owner._logger.warning(
                        "bot_slow_handler",
                        handler=name,
                        duration_seconds=elapsed,
                        outcome=outcome,
                    )


class BotDiagnostics:
    """Bounded counters and optional sampler; interval zero installs nothing.

    Construct one instance per bot process. Attachments are owned by this instance;
    Redis attachment must target a process-owned async client, never a shared class.
    Call ``stop`` in a finally block before disposing engines/clients. Instances are
    single-use after stop. ``snapshot`` is also available for deterministic sampling
    and drains interval data without logging it.
    """

    def __init__(self, interval_seconds: float = 0, slow_handler_seconds: float = 1.0) -> None:
        if interval_seconds < 0 or slow_handler_seconds < 0:
            raise ValueError("diagnostic intervals must be nonnegative")
        self.interval_seconds = interval_seconds
        self.slow_handler_seconds = slow_handler_seconds
        self._stopped = False
        self._task: asyncio.Task[None] | None = None
        self._engines: list[tuple[Any, list[tuple[str, Callable[..., Any]]]]] = []
        self._redis: list[tuple[Any, Any, bool, Any]] = []
        self.request_middleware = _RequestMiddleware(self)
        self.handler_middleware = _HandlerMiddleware(self)
        if not self.enabled:
            return
        self._logger = get_logger(__name__)
        self._poll = _Operations()
        self._telegram_api = _Operations()
        self._handlers = _Operations()
        self._handler_timings = {"other": _Operations()}
        self._lag = 0.0
        self._database = _Operations()
        self._redis_operations = _Operations()
        self._updates = 0
        self._poll_requests = 0
        self._poll_retries = 0
        self._retry_after = 0
        self._poll_failed = False
        self._slow_handlers = 0
        self._slow_warning_sent = False
        self._wall = time.monotonic()
        self._cpu = time.process_time()

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0 and not self._stopped

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._wall = time.monotonic()
            self._cpu = time.process_time()
            self._task = asyncio.create_task(self._sample(), name="bot-diagnostics")

    async def _sample(self) -> None:
        tick = min(1.0, self.interval_seconds)
        next_snapshot = time.monotonic() + self.interval_seconds
        while True:
            deadline = time.monotonic() + tick
            await asyncio.sleep(tick)
            now = time.monotonic()
            self._lag = max(self._lag, now - deadline)
            if now >= next_snapshot:
                self._logger.info("bot_diagnostics", **self.snapshot())
                next_snapshot = now + self.interval_seconds

    def snapshot(self, *, lag_seconds: float = 0.0) -> dict[str, Any]:
        if not self.enabled:
            return {}
        wall, cpu = time.monotonic(), time.process_time()
        elapsed = max(0.0, wall - self._wall)
        cpu_delta = max(0.0, cpu - self._cpu)
        self._wall, self._cpu = wall, cpu
        try:
            task_count = len(asyncio.all_tasks())
        except RuntimeError:
            task_count = None
        result = {
            "interval_seconds": elapsed,
            "process_cpu_seconds": cpu_delta,
            "process_cpu_percent": 100 * cpu_delta / elapsed if elapsed else 0.0,
            "process_rss_bytes": rss_bytes(),
            "event_loop_lag_seconds": max(self._lag, lag_seconds, 0.0),
            "asyncio_tasks": task_count,
            "duration_bucket_upper_seconds": list(_DURATION_BOUNDS),
            "poll": {
                **self._poll.drain(),
                "requests": self._poll_requests,
                "requests_per_second": self._poll_requests / elapsed if elapsed else 0.0,
                "retries": self._poll_retries,
                "retries_per_second": self._poll_retries / elapsed if elapsed else 0.0,
                "retry_after": self._retry_after,
                "updates": self._updates,
                "updates_per_second": self._updates / elapsed if elapsed else 0.0,
            },
            "telegram_api": self._telegram_api.drain(),
            "handlers": {**self._handlers.drain(), "slow": self._slow_handlers},
            "handler_timings": {
                name: timing.drain() for name, timing in self._handler_timings.items()
            },
            "database": self._database.drain(),
            "redis": self._redis_operations.drain(),
        }
        self._updates = self._poll_retries = self._retry_after = self._slow_handlers = 0
        self._poll_requests = 0
        self._slow_warning_sent = False
        self._lag = 0.0
        return result

    def attach_engine(self, engine: Any) -> None:
        if not self.enabled:
            return
        target = getattr(engine, "sync_engine", engine)
        if any(existing is target for existing, _ in self._engines):
            return
        # Context identity prevents collisions with other diagnostic instances.
        attribute = f"_bot_diagnostics_{id(self)}"

        def before(
            conn: Any,
            cursor: Any,
            statement: Any,
            parameters: Any,
            context: Any,
            executemany: Any,
        ) -> None:
            setattr(context, attribute, time.monotonic())
            self._database.active += 1

        def finish(context: Any, outcome: str) -> None:
            if context is not None and hasattr(context, attribute):
                start = getattr(context, attribute)
                delattr(context, attribute)
                self._database.finish(start, outcome)

        def after(
            conn: Any,
            cursor: Any,
            statement: Any,
            parameters: Any,
            context: Any,
            executemany: Any,
        ) -> None:
            finish(context, "success")

        def failed(context: Any) -> None:
            outcome = (
                "cancelled"
                if isinstance(context.original_exception, asyncio.CancelledError)
                else "error"
            )
            finish(context.execution_context, outcome)

        listeners: list[tuple[str, Callable[..., Any]]] = [
            ("before_cursor_execute", before),
            ("after_cursor_execute", after),
            ("handle_error", failed),
        ]
        attached: list[tuple[str, Callable[..., Any]]] = []
        try:
            for name, callback in listeners:
                event.listen(target, name, callback)
                attached.append((name, callback))
        except Exception:
            for name, callback in reversed(attached):
                event.remove(target, name, callback)
            raise
        self._engines.append((target, listeners))

    def attach_redis(self, client: Any) -> None:
        if (
            not self.enabled
            or client is None
            or any(existing is client for existing, *_ in self._redis)
        ):
            return
        original = client.execute_command
        had_override = "execute_command" in vars(client)
        override = vars(client).get("execute_command")

        async def execute(*args: Any, **kwargs: Any) -> Any:
            if not self.enabled:
                return await original(*args, **kwargs)
            self._redis_operations.active += 1
            start = time.monotonic()
            outcome = "error"
            try:
                result = await original(*args, **kwargs)
                outcome = "success"
                return result
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                self._redis_operations.finish(start, outcome)

        client.execute_command = execute
        self._redis.append((client, execute, had_override, override))

    async def stop(self) -> None:
        self._stopped = True
        try:
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        finally:
            self._task = None
            for target, listeners in reversed(self._engines):
                for name, callback in reversed(listeners):
                    event.remove(target, name, callback)
            self._engines.clear()
            for client, wrapper, had_override, override in reversed(self._redis):
                # Do not overwrite a wrapper another owner installed afterward.
                if client.execute_command is wrapper:
                    if had_override:
                        client.execute_command = override
                    else:
                        del client.execute_command
            self._redis.clear()
