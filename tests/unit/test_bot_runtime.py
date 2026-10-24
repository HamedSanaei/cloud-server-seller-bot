"""Exercise the locked SDK transport/scheduler, not polling call wiring."""

from __future__ import annotations

import asyncio
import gc
import warnings
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import Update, User
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from cloud_platform.bot.runtime import BoundedDispatcher


class TelegramSession(BaseSession):
    def __init__(self, responses=()):
        super().__init__()
        self.responses = iter(responses)
        self.requests = []
        self.idle = asyncio.Event()
        self.closed = False

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 - SDK interface
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="fixture")
        assert isinstance(method, GetUpdates)
        self.requests.append((method.offset, method.timeout, timeout))
        response = next(self.responses, None)
        if response is None:
            self.idle.set()
            await asyncio.Future()  # Model Telegram holding the long poll open.
        if isinstance(response, Exception):
            raise response
        return response

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b""


async def test_idle_poll_waits_without_spawning_handlers_or_reissuing_requests():
    session = TelegramSession()
    bot = Bot("123456:fixture", session=session)
    dispatcher = BoundedDispatcher()
    task = asyncio.create_task(
        dispatcher._polling(bot, polling_timeout=30, tasks_concurrency_limit=4)
    )
    await asyncio.wait_for(session.idle.wait(), 2)
    for _ in range(10):
        await asyncio.sleep(0)
    assert session.requests == [(None, 30, 90)]
    assert not dispatcher._handle_update_tasks
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await dispatcher.drain_handlers(1)
    await bot.session.close()
    assert session.closed


async def test_burst_backpressure_cancellation_and_no_unawaited_coroutine():
    session = TelegramSession([[Update(update_id=i) for i in range(100)]])
    dispatcher = BoundedDispatcher()
    started = asyncio.Event()
    active = 0
    peak = 0
    cancelled = 0

    async def process(bot, update, **kwargs):
        nonlocal active, peak, cancelled
        active += 1
        peak = max(peak, active)
        if active == 4:
            started.set()
        try:
            await asyncio.Future()
        finally:
            active -= 1
            cancelled += 1

    dispatcher._process_update = process
    bot = Bot("123456:fixture", session=session)
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always", RuntimeWarning)
        polling = asyncio.create_task(dispatcher._polling(bot, tasks_concurrency_limit=4))
        await asyncio.wait_for(started.wait(), 2)
        assert len(dispatcher._handle_update_tasks) == 4
        assert len(session.requests) == 1  # No fetching past saturated batch.
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
        await dispatcher.drain_handlers(0)
        gc.collect()
    assert peak == 4 and active == 0 and cancelled == 4
    assert not dispatcher._handle_update_tasks
    assert not [w for w in emitted if "never awaited" in str(w.message)]
    await bot.session.close()


@pytest.mark.parametrize("failure", ["network", "conflict", "429"])
async def test_api_failures_wait_and_resume_with_offset(monkeypatch, failure):
    method = GetUpdates()
    errors = {
        "network": TelegramNetworkError(method, "fixture offline"),
        "conflict": TelegramConflictError(method, "fixture conflict"),
        "429": TelegramRetryAfter(method, "fixture limited", retry_after=17),
    }
    session = TelegramSession([errors[failure], [Update(update_id=10)], [Update(update_id=11)]])
    bot = Bot("123456:fixture", session=session)
    sleep = AsyncMock()
    monkeypatch.setattr("cloud_platform.bot.runtime.asyncio.sleep", sleep)
    updates = BoundedDispatcher._listen_updates(bot)
    assert (await anext(updates)).update_id == 10
    assert (await anext(updates)).update_id == 11
    delay = sleep.await_args.args[0]
    assert delay >= (17 if failure == "429" else 1)
    assert delay <= (17 if failure == "429" else 5)
    assert [r[0] for r in session.requests] == [None, None, 11]
    await updates.aclose()
    await bot.session.close()


@pytest.mark.parametrize("dependency", ["redis", "postgresql"])
async def test_dependency_outage_is_not_replayed_and_other_updates_complete(dependency):
    session = TelegramSession([[Update(update_id=i) for i in range(8)]])
    bot = Bot("123456:fixture", session=session)
    dispatcher = BoundedDispatcher()
    seen = []

    async def feed(bot, update, **kwargs):
        seen.append(update.update_id)
        if update.update_id == 2:
            if dependency == "redis":
                raise RedisConnectionError("fixture connection refused")
            raise OperationalError(
                "SELECT 1", {}, ConnectionError("fixture PostgreSQL connection refused")
            )

    dispatcher.feed_update = feed
    polling = asyncio.create_task(dispatcher._polling(bot, tasks_concurrency_limit=2))
    await asyncio.wait_for(session.idle.wait(), 2)
    await dispatcher.drain_handlers(1)
    polling.cancel()
    await asyncio.gather(polling, return_exceptions=True)
    assert sorted(seen) == list(range(8))
    assert session.requests[-1][0] == 8
    await bot.session.close()


async def test_graceful_drain_finishes_accepted_handlers_before_resource_close():
    dispatcher = BoundedDispatcher()
    release = asyncio.Event()
    finished = []

    async def work():
        await release.wait()
        finished.append("done")

    task = asyncio.create_task(work())
    dispatcher._handle_update_tasks.add(task)
    task.add_done_callback(dispatcher._handle_update_tasks.discard)
    draining = asyncio.create_task(dispatcher.drain_handlers(2))
    release.set()
    await draining
    assert finished == ["done"] and not dispatcher._handle_update_tasks


async def test_repeated_api_outage_has_bounded_backoff_and_success_resets_it(monkeypatch):
    failure = TelegramNetworkError(GetUpdates(), "fixture offline")
    session = TelegramSession(
        [failure] * 20 + [[Update(update_id=10)], failure, [Update(update_id=11)]]
    )
    bot = Bot("123456:fixture", session=session)
    sleep = AsyncMock()
    monkeypatch.setattr("cloud_platform.bot.runtime.asyncio.sleep", sleep)
    updates = BoundedDispatcher._listen_updates(bot)
    assert (await anext(updates)).update_id == 10
    delays = [call.args[0] for call in sleep.await_args_list]
    assert len(delays) == 20 and all(1 <= delay <= 5 for delay in delays)
    assert delays[-1] == 5
    assert (await anext(updates)).update_id == 11
    assert 1 <= sleep.await_args_list[-1].args[0] <= 1.1
    await updates.aclose()
    await bot.session.close()


async def test_external_cancellation_stops_intake_before_draining():
    session = TelegramSession([[Update(update_id=i) for i in range(100)]])
    bot = Bot("123456:fixture", session=session)
    dispatcher = BoundedDispatcher(disable_fsm=True)
    started = asyncio.Event()
    seen = []

    async def process(bot, update, **kwargs):
        seen.append(update.update_id)
        if len(seen) == 2:
            started.set()
        await asyncio.Future()

    dispatcher._process_update = process
    polling = asyncio.create_task(
        dispatcher.start_polling(
            bot, handle_signals=False, close_bot_session=False, tasks_concurrency_limit=2
        )
    )
    await asyncio.wait_for(started.wait(), 2)
    polling.cancel()
    await asyncio.gather(polling, return_exceptions=True)
    assert not dispatcher._intake_tasks
    await dispatcher.drain_handlers(0)
    for _ in range(5):
        await asyncio.sleep(0)
    assert seen == [0, 1]
    assert not dispatcher._handle_update_tasks
    # Intake scheduled after shutdown is also refused.
    await dispatcher._polling(bot, tasks_concurrency_limit=2)
    assert len(session.requests) == 1
    await bot.session.close()
