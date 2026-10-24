"""Diagnostics use bounded anonymous aggregates, never events or exception strings."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.handler import HandlerObject
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetMe, GetUpdates
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError

from cloud_platform.bot import diagnostics

# Synthetic leak sentinel, not a credential.
SECRET = "private-token-chat-user-payload-sql-redis-key"  # pragma: allowlist secret


class Clock:
    def __init__(self):
        self.wall = 100.0
        self.cpu = 5.0

    def monotonic(self):
        return self.wall

    def process_time(self):
        return self.cpu

    def advance(self, wall, cpu=0):
        self.wall += wall
        self.cpu += cpu


class Logger:
    def __init__(self):
        self.records = []

    def info(self, event, **fields):
        self.records.append(("info", event, fields))

    def warning(self, event, **fields):
        self.records.append(("warning", event, fields))


@pytest.fixture
def environment(monkeypatch):
    clock, logger = Clock(), Logger()
    monkeypatch.setattr(diagnostics, "time", clock)
    monkeypatch.setattr(diagnostics, "get_logger", lambda name: logger)
    monkeypatch.setattr(diagnostics, "rss_bytes", lambda: 123456)
    return clock, logger


async def test_disabled_has_no_sampling_instrumentation_or_clock_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("disabled diagnostics must not sample or attach hooks")

    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(monotonic=forbidden))
    monkeypatch.setattr(diagnostics, "get_logger", forbidden)
    monkeypatch.setattr(diagnostics.event, "listen", forbidden)
    owner = diagnostics.BotDiagnostics(interval_seconds=0)
    owner.start()
    owner.attach_engine(object())
    owner.attach_redis(object())
    assert owner._task is None
    assert not owner.enabled
    assert owner.snapshot() == {}

    async def request(bot, method):
        return SECRET

    async def handler(event, data):
        return SECRET

    assert await owner.request_middleware(request, None, GetUpdates()) == SECRET
    assert await owner.handler_middleware(handler, None, {}) == SECRET
    await owner.stop()


async def test_poll_outcomes_retries_update_rate_and_secret_absence(environment):
    clock, logger = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    method = GetUpdates()

    async def success(bot, method):
        clock.advance(0.5)
        return [SECRET, SECRET]

    async def failure(bot, method):
        clock.advance(0.25)
        raise RuntimeError(SECRET)

    async def throttled(bot, method):
        clock.advance(0.25)
        raise TelegramRetryAfter(method=method, message=SECRET, retry_after=3)

    async def cancelled(bot, method):
        clock.advance(0.5)
        raise asyncio.CancelledError(SECRET)

    await owner.request_middleware(success, None, method)
    with pytest.raises(RuntimeError, match=SECRET):
        await owner.request_middleware(failure, None, method)
    with pytest.raises(TelegramRetryAfter):
        await owner.request_middleware(throttled, None, method)
    await owner.request_middleware(success, None, method)
    with pytest.raises(asyncio.CancelledError):
        await owner.request_middleware(cancelled, None, method)
    clock.advance(8, cpu=2)
    snapshot = owner.snapshot(lag_seconds=0.125)
    poll = snapshot["poll"]
    assert poll["active"] == 0
    assert poll["completed"] == 5
    assert poll["requests"] == 5
    assert poll["requests_per_second"] == 0.5
    assert poll["retries_per_second"] == 0.2
    assert poll["success"] == 2
    assert poll["failed"] == poll["error"] == 2
    assert poll["cancelled"] == 1
    assert poll["retries"] == 2
    assert poll["retry_after"] == 1
    assert poll["updates"] == 4
    assert poll["updates_per_second"] == 0.4
    assert poll["duration"]["sum_seconds"] == 2
    assert snapshot["process_cpu_percent"] == 20
    assert snapshot["process_cpu_seconds"] == 2
    assert snapshot["process_rss_bytes"] == 123456
    assert snapshot["event_loop_lag_seconds"] == 0.125
    assert snapshot["asyncio_tasks"] >= 1
    assert SECRET not in json.dumps(snapshot)
    assert logger.records == []
    reset = owner.snapshot()
    assert reset["poll"]["completed"] == reset["poll"]["updates"] == 0
    assert reset["poll"]["duration"]["count"] == 0
    await owner.stop()


async def test_non_polling_telegram_calls_have_fixed_anonymous_aggregate(environment):
    clock, _ = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)

    async def request(bot, method):
        clock.advance(0.25)
        return SECRET

    async def failure(bot, method):
        clock.advance(0.5)
        raise RuntimeError(SECRET)

    assert await owner.request_middleware(request, None, GetMe()) == SECRET
    with pytest.raises(RuntimeError):
        await owner.request_middleware(failure, None, GetMe())
    snapshot = owner.snapshot()
    assert snapshot["telegram_api"]["completed"] == 2
    assert snapshot["telegram_api"]["success"] == 1
    assert snapshot["telegram_api"]["failed"] == 1
    assert snapshot["telegram_api"]["duration"]["sum_seconds"] == 0.75
    assert snapshot["poll"]["completed"] == snapshot["poll"]["retries"] == 0
    assert SECRET not in json.dumps(snapshot)
    await owner.stop()


async def test_slow_failed_handler_uses_selected_static_name_not_wrapper_or_data(environment):
    clock, logger = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10, slow_handler_seconds=1)

    async def selected_handler(event, data):
        return None

    async def middleware_wrapper(event, data):
        clock.advance(2)
        raise RuntimeError(SECRET)

    selected_handler.__name__ = SECRET
    data = {"handler": HandlerObject(selected_handler), "payload": SECRET}
    for _ in range(3):
        with pytest.raises(RuntimeError, match=SECRET):
            await owner.handler_middleware(middleware_wrapper, SECRET, data)
    snapshot = owner.snapshot()
    assert snapshot["handlers"]["active"] == 0
    assert snapshot["handlers"]["failed"] == snapshot["handlers"]["slow"] == 3
    assert snapshot["handlers"]["duration"]["max_seconds"] == 2
    assert logger.records == [
        (
            "warning",
            "bot_slow_handler",
            {"handler": "selected_handler", "duration_seconds": 2, "outcome": "error"},
        )
    ]
    assert SECRET not in json.dumps([snapshot, logger.records])
    # Draining an interval permits one subsequent warning, not one per handler.
    with pytest.raises(RuntimeError):
        await owner.handler_middleware(middleware_wrapper, SECRET, data)
    assert len(logger.records) == 2
    await owner.stop()


async def test_handler_cancellation_is_not_failure_and_releases_active_gauge(environment):
    clock, logger = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10, slow_handler_seconds=10)
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(event, data):
        entered.set()
        await release.wait()

    task = asyncio.create_task(owner.handler_middleware(handler, None, {}))
    await entered.wait()
    assert owner.snapshot()["handlers"]["active"] == 1
    clock.advance(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    handlers = owner.snapshot()["handlers"]
    assert handlers["active"] == handlers["failed"] == 0
    assert handlers["completed"] == handlers["cancelled"] == 1
    assert handlers["duration"]["sum_seconds"] == 0.5
    assert logger.records == []
    await owner.stop()


async def test_successful_handler_returns_original_result(environment):
    clock, _ = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    result = object()

    async def handler(event, data):
        clock.advance(0.125)
        return result

    assert await owner.handler_middleware(handler, None, {}) is result
    handlers = owner.snapshot()["handlers"]
    assert handlers["success"] == handlers["completed"] == 1
    assert handlers["failed"] == handlers["cancelled"] == 0
    await owner.stop()


def test_duration_histogram_is_fixed_and_p95_is_conservative_upper_bound():
    duration = diagnostics._Duration()
    for _ in range(95):
        duration.observe(0.002)
    for _ in range(5):
        duration.observe(120)
    sample = duration.drain()
    assert sample["count"] == 100
    assert sample["sum_seconds"] == pytest.approx(600.19)
    assert sample["p95_upper_seconds"] == 0.005
    assert sample["max_seconds"] == 120
    assert len(sample["buckets"]) == len(diagnostics._DURATION_BOUNDS) + 1
    assert sum(sample["buckets"]) == 100
    assert duration.drain()["count"] == 0
    duration.observe(120)
    assert duration.drain()["p95_upper_seconds"] == 120
    assert duration.drain()["p95_upper_seconds"] is None


async def test_engine_hooks_measure_success_failure_and_remove_only_owned_listeners(environment):
    clock, logger = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    engine = create_engine("sqlite://")
    external_calls = []

    def external_listener(*args):
        external_calls.append(True)

    def external_after(*args):
        clock.advance(0.125)

    event.listen(engine, "before_cursor_execute", external_listener)
    event.listen(engine, "after_cursor_execute", external_after)
    owner.attach_engine(SimpleNamespace(sync_engine=engine))
    owner.attach_engine(engine)
    owned_listeners = list(owner._engines[0][1])
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT :secret"), {"secret": SECRET}).scalar() == SECRET
            with pytest.raises(DBAPIError) as failure:
                connection.execute(text(f"SELECT * FROM {SECRET.replace('-', '_')}"))
            assert failure.value.orig is not None
        snapshot = owner.snapshot()
        assert snapshot["database"]["completed"] == 2
        assert snapshot["database"]["success"] == snapshot["database"]["failed"] == 1
        assert snapshot["database"]["active"] == 0
        assert snapshot["database"]["duration"]["sum_seconds"] == 0.125
        assert SECRET not in json.dumps([snapshot, logger.records])
        await owner.stop()
        await owner.stop()
        assert event.contains(engine, "before_cursor_execute", external_listener)
        for name, callback in owned_listeners:
            assert not event.contains(engine, name, callback)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        assert len(external_calls) == 3
    finally:
        await owner.stop()
        event.remove(engine, "before_cursor_execute", external_listener)
        event.remove(engine, "after_cursor_execute", external_after)
        engine.dispose()


class RedisClient:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    async def execute_command(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.clock.advance(0.25)
        if kwargs.get("fail"):
            raise RedisConnectionError(SECRET)
        if kwargs.get("cancel"):
            raise asyncio.CancelledError(SECRET)
        return SECRET


async def test_redis_wrapper_is_instance_local_transparent_and_removed_on_shutdown(environment):
    clock, logger = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    client, other_client = RedisClient(clock), RedisClient(clock)
    owner.attach_redis(None)
    owner.attach_redis(client)
    wrapper = client.execute_command
    owner.attach_redis(client)
    assert client.execute_command is wrapper
    assert "execute_command" not in vars(other_client)
    assert await client.execute_command("GET", SECRET) == SECRET
    with pytest.raises(RedisConnectionError):
        await client.execute_command("SET", SECRET, fail=True)
    with pytest.raises(asyncio.CancelledError):
        await client.execute_command("GET", SECRET, cancel=True)
    assert client.calls[0] == (("GET", SECRET), {})
    snapshot = owner.snapshot()
    assert snapshot["redis"]["completed"] == 3
    assert snapshot["redis"]["active"] == 0
    assert snapshot["redis"]["success"] == snapshot["redis"]["failed"] == 1
    assert snapshot["redis"]["cancelled"] == 1
    assert snapshot["redis"]["duration"]["sum_seconds"] == 0.75
    assert SECRET not in json.dumps([snapshot, logger.records])
    await owner.stop()
    assert "execute_command" not in vars(client)
    assert await client.execute_command("GET", SECRET) == SECRET


async def test_shutdown_preserves_preexisting_and_later_redis_instance_overrides(environment):
    clock, _ = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    first, second = RedisClient(clock), RedisClient(clock)

    async def original(*args, **kwargs):
        return "original"

    async def later(*args, **kwargs):
        return "later"

    first.execute_command = original
    owner.attach_redis(first)
    owner.attach_redis(second)
    second.execute_command = later
    await owner.stop()
    assert first.execute_command is original
    assert second.execute_command is later


async def test_sampler_reports_cpu_rss_lag_and_cancels_pending_sleep(environment, monkeypatch):
    clock, logger = environment
    sleeping = asyncio.Event()
    sleep_cancelled = asyncio.Event()
    calls = 0

    async def sleep(seconds):
        nonlocal calls
        calls += 1
        assert seconds == 1
        if calls <= 10:
            # An early stall must survive nine subsequent on-time wakeups.
            clock.advance(seconds + (0.25 if calls == 1 else 0), cpu=0.5 if calls == 1 else 0)
            return
        sleeping.set()
        try:
            await asyncio.Future()
        finally:
            sleep_cancelled.set()

    monkeypatch.setattr(
        diagnostics,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            sleep=sleep,
            all_tasks=asyncio.all_tasks,
            gather=asyncio.gather,
            CancelledError=asyncio.CancelledError,
        ),
    )
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    owner.start()
    task = owner._task
    owner.start()
    assert owner._task is task
    await sleeping.wait()
    assert len(logger.records) == 1
    level, event_name, fields = logger.records[0]
    assert (level, event_name) == ("info", "bot_diagnostics")
    assert fields["event_loop_lag_seconds"] == 0.25
    assert fields["interval_seconds"] == 10.25
    assert fields["process_cpu_percent"] == pytest.approx(100 * 0.5 / 10.25)
    assert fields["process_rss_bytes"] == 123456
    assert fields["asyncio_tasks"] >= 2
    await owner.stop()
    assert sleep_cancelled.is_set()
    assert task.cancelled()
    assert owner._task is None
    assert not owner.enabled
    owner.start()
    assert owner._task is None
    assert SECRET not in json.dumps(logger.records)


def test_linux_rss_uses_current_resident_pages(monkeypatch):
    import io
    import os

    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(diagnostics.Path, "open", lambda *args, **kwargs: io.StringIO("100 7 2"))
    monkeypatch.setattr(os, "sysconf", lambda name: 4096, raising=False)
    assert diagnostics.rss_bytes() == 7 * 4096


def test_rss_unavailable_is_explicit_not_fake_zero(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(diagnostics.Path, "open", unavailable)
    assert diagnostics.rss_bytes() is None


@pytest.mark.parametrize("succeeds", [True, False])
def test_windows_rss_uses_current_working_set_without_optional_dependencies(monkeypatch, succeeds):
    import ctypes

    def current_process():
        return 123

    def process_memory(handle, pointer, size):
        assert handle == 123
        assert size == ctypes.sizeof(pointer._obj)
        pointer._obj.working_set = 8192
        return succeeds

    libraries = {
        "kernel32": SimpleNamespace(GetCurrentProcess=current_process),
        "psapi": SimpleNamespace(GetProcessMemoryInfo=process_memory),
    }
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: libraries[name], raising=False)
    monkeypatch.setattr(diagnostics.sys, "platform", "win32")
    assert diagnostics.rss_bytes() == (8192 if succeeds else None)


async def test_blocked_handler_delays_real_loop_timer_and_diagnostic_sample(monkeypatch):
    import time

    logger = Logger()
    sampled = asyncio.Event()
    original_info = logger.info

    def sampled_info(event_name, **fields):
        original_info(event_name, **fields)
        sampled.set()

    logger.info = sampled_info
    monkeypatch.setattr(diagnostics, "get_logger", lambda name: logger)
    monkeypatch.setattr(diagnostics, "rss_bytes", lambda: 123456)
    owner = diagnostics.BotDiagnostics(interval_seconds=0.01, slow_handler_seconds=1)
    loop = asyncio.get_running_loop()
    timer_delays = []

    async def blocking_handler(event, data):
        time.sleep(0.05)  # noqa: ASYNC251 -- intentional event-loop starvation fixture

    owner.start()
    timer = None
    try:
        # Let the sampler establish its deadline before the handler monopolizes
        # the loop; an independently scheduled timer provides direct lag evidence.
        await asyncio.sleep(0)
        target = loop.time() + 0.005
        timer = loop.call_at(target, lambda: timer_delays.append(loop.time() - target))
        await owner.handler_middleware(blocking_handler, None, {})
        await asyncio.wait_for(sampled.wait(), timeout=2)
        assert timer_delays and timer_delays[0] >= 0.02
        samples = [fields for level, name, fields in logger.records if name == "bot_diagnostics"]
        assert samples[0]["event_loop_lag_seconds"] >= 0.02
        assert samples[0]["handlers"]["success"] == 1
        assert samples[0]["handlers"]["duration"]["max_seconds"] >= 0.04
    finally:
        if timer is not None:
            timer.cancel()
        await owner.stop()


async def test_selected_handler_timings_distinguish_slow_and_fast_handlers(environment):
    clock, _ = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)

    async def slow_selected(event, data):
        clock.advance(2)

    async def fast_selected(event, data):
        clock.advance(0.125)

    async def wrapper(event, data):
        return await data["handler"].callback(event, data)

    for callback in (slow_selected, fast_selected, fast_selected):
        await owner.handler_middleware(wrapper, None, {"handler": HandlerObject(callback)})
    snapshot = owner.snapshot()
    slow = snapshot["handler_timings"]["slow_selected"]
    fast = snapshot["handler_timings"]["fast_selected"]
    assert slow["completed"] == slow["success"] == 1
    assert slow["duration"]["sum_seconds"] == slow["duration"]["max_seconds"] == 2
    assert fast["completed"] == fast["success"] == 2
    assert fast["duration"]["sum_seconds"] == 0.25
    assert fast["duration"]["max_seconds"] == 0.125
    assert "wrapper" not in snapshot["handler_timings"]
    assert owner.snapshot()["handler_timings"]["slow_selected"]["completed"] == 0
    await owner.stop()


async def test_handler_timing_cardinality_is_capped_with_static_name_overflow(environment):
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    for index in range(70):
        namespace = {}
        exec(f"async def selected_{index}(event, data):\n    return None\n", namespace)
        callback = namespace[f"selected_{index}"]
        await owner.handler_middleware(callback, None, {"handler": HandlerObject(callback)})
    timings = owner.snapshot()["handler_timings"]
    assert len(timings) == 64
    assert timings["other"]["completed"] == 7
    assert sum(timing["completed"] for timing in timings.values()) == 70
    assert "selected_62" in timings
    assert "selected_63" not in timings
    # The lifetime cap remains bounded across interval drains.
    await owner.handler_middleware(callback, None, {"handler": HandlerObject(callback)})
    timings = owner.snapshot()["handler_timings"]
    assert len(timings) == 64
    assert timings["other"]["completed"] == 1
    await owner.stop()


async def test_engine_execution_cancellation_releases_active_without_counting_failure(environment):
    clock, logger = environment
    owner = diagnostics.BotDiagnostics(interval_seconds=10)
    engine = create_engine("sqlite://")
    cancellation = asyncio.CancelledError(SECRET)

    def cancel_execution(cursor, statement, parameters, context):
        clock.advance(0.25)
        raise cancellation

    owner.attach_engine(engine)
    event.listen(engine.dialect, "do_execute", cancel_execution)
    try:
        with engine.connect() as connection:
            with pytest.raises(asyncio.CancelledError) as caught:
                connection.execute(text("SELECT :secret"), {"secret": SECRET})
        assert caught.value is cancellation
        snapshot = owner.snapshot()
        database = snapshot["database"]
        assert database["active"] == 0
        assert database["completed"] == database["cancelled"] == 1
        assert database["failed"] == database["error"] == database["success"] == 0
        assert database["duration"]["count"] == 1
        assert database["duration"]["sum_seconds"] == 0.25
        assert SECRET not in json.dumps([snapshot, logger.records])
    finally:
        await owner.stop()
        event.remove(engine.dialect, "do_execute", cancel_execution)
        engine.dispose()
