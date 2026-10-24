"""Single-consumer polling with bounded work and cancellation-safe shutdown."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiogram.utils.backoff import BackoffConfig

from cloud_platform.core.logging import get_logger

logger = get_logger(__name__)


class BoundedDispatcher(Dispatcher):
    """Keep backpressure before task creation; own handler shutdown explicitly.

    Overrides the aiogram 3 polling extension points. Contract tests exercise
    these against the locked SDK, including cancellation at a full semaphore.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._intake_tasks: set[asyncio.Task[Any]] = set()
        self._intake_stopped = False

    async def start_polling(self, *bots: Bot, **kwargs: Any) -> None:
        try:
            await super().start_polling(*bots, **kwargs)
        finally:
            # External cancellation skips aiogram's pending-task cleanup.
            await self.stop_intake()

    async def stop_intake(self) -> None:
        self._intake_stopped = True
        if self._stop_signal is not None:
            self._stop_signal.set()
        tasks = tuple(self._intake_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @classmethod
    async def _listen_updates(
        cls,
        bot: Bot,
        polling_timeout: int = 30,
        backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
        allowed_updates: list[str] | None = None,
    ) -> AsyncGenerator[Update]:
        request = GetUpdates(timeout=polling_timeout, allowed_updates=allowed_updates)
        delay = backoff_config.min_delay
        failures = 0
        while True:
            try:
                updates = await bot(
                    request, request_timeout=int((bot.session.timeout or 60) + polling_timeout)
                )
            except Exception as error:
                failures += 1
                # Respect the API's deadline. Network/conflict retries use bounded
                # exponential backoff with positive jitter, never a tight loop.
                wait = (
                    max(float(error.retry_after), delay)
                    if isinstance(error, TelegramRetryAfter)
                    else min(
                        backoff_config.max_delay,
                        delay + random.uniform(0, backoff_config.jitter),
                    )
                )
                logger.warning(
                    "bot_poll_retry",
                    outcome=type(error).__name__,
                    retry_seconds=wait,
                    consecutive_failures=failures,
                )
                await asyncio.sleep(wait)
                delay = min(backoff_config.max_delay, delay * backoff_config.factor)
                continue
            delay = backoff_config.min_delay
            failures = 0
            for update in updates:
                yield update
                request.offset = update.update_id + 1

    async def _polling(
        self,
        bot: Bot,
        polling_timeout: int = 30,
        handle_as_tasks: bool = True,
        backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
        allowed_updates: list[str] | None = None,
        tasks_concurrency_limit: int | None = None,
        **kwargs: Any,
    ) -> None:
        current = asyncio.current_task()
        if current is None or self._intake_stopped:
            return
        self._intake_tasks.add(current)
        try:
            await self._consume_updates(
                bot,
                polling_timeout,
                handle_as_tasks,
                backoff_config,
                allowed_updates,
                tasks_concurrency_limit,
                **kwargs,
            )
        finally:
            self._intake_tasks.discard(current)

    async def _consume_updates(
        self,
        bot: Bot,
        polling_timeout: int,
        handle_as_tasks: bool,
        backoff_config: BackoffConfig,
        allowed_updates: list[str] | None,
        tasks_concurrency_limit: int | None,
        **kwargs: Any,
    ) -> None:
        if not handle_as_tasks or tasks_concurrency_limit is None or tasks_concurrency_limit < 1:
            raise ValueError("bounded polling requires a positive concurrency limit")
        # Verify identity once, using the shared bot session; never log it.
        await bot.me()
        slots = asyncio.Semaphore(tasks_concurrency_limit)

        async def process(update: Update) -> None:
            try:
                await self._process_update(bot, update, **kwargs)
            finally:
                slots.release()

        async for update in self._listen_updates(
            bot, polling_timeout, backoff_config, allowed_updates
        ):
            await slots.acquire()
            # Construct the coroutine only AFTER acquiring a slot. Cancellation
            # while waiting must not leak an unawaited SDK update coroutine.
            task = asyncio.create_task(process(update), name="telegram-update")
            self._handle_update_tasks.add(task)
            task.add_done_callback(self._handle_update_tasks.discard)

    async def drain_handlers(self, grace_seconds: float) -> None:
        """Finish accepted work before closing HTTP/DB/Redis; cancel at deadline."""
        tasks = tuple(self._handle_update_tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
