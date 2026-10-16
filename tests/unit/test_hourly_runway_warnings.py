"""User-wide 72h runway forecasts and durable six-hour delivery cadence."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.notifications.domain import TelegramLowBalanceNotifier
from cloud_platform.modules.notifications.hourly import (
    HourlyRunwayWarningService,
    forecast_exhaustion,
)
from tests.unit.test_prepaid_hourly_billing import server

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


class Lock:
    acquired = True

    @asynccontextmanager
    async def guard(self):
        yield self.acquired


@pytest.mark.parametrize("balance, hours", [(0, 1), (70_000, 71), (71_000, None)])
def test_exact_72h_threshold_includes_already_paid_time(balance, hours):
    result = forecast_exhaustion(balance, [(uuid4(), 1_000, NOW + timedelta(hours=1))], NOW)
    assert result == (NOW + timedelta(hours=hours) if hours is not None else None)


def test_aggregates_staggered_active_servers_not_individual_wallet_runways():
    periods = [
        (uuid4(), 100, NOW + timedelta(minutes=15)),
        (uuid4(), 200, NOW + timedelta(minutes=45)),
    ]
    assert forecast_exhaustion(299, periods, NOW) == NOW + timedelta(minutes=45)
    assert forecast_exhaustion(300, periods, NOW) == NOW + timedelta(hours=1, minutes=15)


def setup_warning():
    row = server(NOW + timedelta(hours=1))
    second = server(NOW + timedelta(minutes=30))
    second.user_id = row.user_id
    repo = SimpleNamespace(list_running=AsyncMock(return_value=[row, second]))
    billing = SimpleNamespace(
        spendable_balance=AsyncMock(return_value=100), next_hour_cost=AsyncMock(return_value=100)
    )
    log = SimpleNamespace(last_runway_sent=AsyncMock(return_value=None), record=AsyncMock())
    notifier, lock = AsyncMock(), Lock()
    service = HourlyRunwayWarningService(
        servers=repo, billing=billing, log=log, notifier=notifier, lock=lock
    )
    return service, notifier, log, billing, lock, repo


async def test_one_aggregate_notice_per_user_every_six_hours_even_after_server_change():
    service, notifier, log, _, _, repo = setup_warning()
    assert await service.run(NOW) == 1
    event = notifier.send.await_args.args[0]
    assert event.active_servers == 2 and event.currency == "IRT"
    assert event.runway_hours == Decimal(1)
    log.last_runway_sent.return_value = NOW
    repo.list_running.return_value = repo.list_running.return_value[1:]
    assert await service.run(NOW + timedelta(hours=5, minutes=59)) == 0
    assert await service.run(NOW + timedelta(hours=6)) == 1
    assert notifier.send.await_count == 2


async def test_busy_lock_or_healthy_balance_sends_nothing():
    service, notifier, _, billing, lock, _ = setup_warning()
    lock.acquired = False
    assert await service.run(NOW) == 0
    billing.next_hour_cost.assert_not_awaited()
    lock.acquired = True
    billing.spendable_balance.return_value = 1_000_000
    assert await service.run(NOW) == 0
    notifier.send.assert_not_awaited()


async def test_failed_delivery_is_not_marked_sent_and_retries():
    service, notifier, log, _, _, _ = setup_warning()
    notifier.send.side_effect = RuntimeError("transport unavailable")
    assert await service.run(NOW) == 0
    log.record.assert_not_awaited()
    notifier.send.side_effect = None
    assert await service.run(NOW + timedelta(minutes=1)) == 1
    log.record.assert_awaited_once()


async def test_fx_failure_sends_no_guessed_forecast():
    service, notifier, log, billing, _, _ = setup_warning()
    billing.next_hour_cost.side_effect = RuntimeError("stale FX")
    assert await service.run(NOW) == 0
    notifier.send.assert_not_awaited()
    log.record.assert_not_awaited()


async def test_telegram_warning_mentions_toman_shutdown_deletion_and_topup():
    service, notifier, _, _, _, _ = setup_warning()
    await service.run(NOW)
    event = notifier.send.await_args.args[0]
    bot = AsyncMock()
    users = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(telegram_user_id=123)))
    await TelegramLowBalanceNotifier(bot, users).send(event)
    text = bot.send_message.await_args.kwargs["text"]
    for fragment in ("تومان", "خاموشی", "حذف", "شارژ", "۲۴ ساعت", "2"):
        assert fragment in text
    assert bot.send_message.await_args.kwargs["chat_id"] == 123
