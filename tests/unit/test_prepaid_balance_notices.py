"""Delivery and deduplication of prepaid balance notices."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.billing.service import PrepaidBalanceDecision
from cloud_platform.modules.notifications.domain import (
    LowBalanceNotificationEvent,
    LowBalanceNotificationKind,
    LowBalanceNotifier,
    TelegramLowBalanceNotifier,
    TelegramNotificationDeliveryError,
)

USER = uuid4()
SERVER = uuid4()
EPISODE = datetime(2026, 9, 27, tzinfo=UTC)


class SentLog:
    def __init__(self) -> None:
        self.sent: set[tuple[UUID, LowBalanceNotificationKind, datetime]] = set()

    async def was_sent(
        self, server_id: UUID, kind: LowBalanceNotificationKind, episode: datetime
    ) -> bool:
        return (server_id, kind, episode) in self.sent

    async def record(
        self,
        user_id: UUID,
        server_id: UUID,
        kind: LowBalanceNotificationKind,
        episode: datetime,
        balance_minor: int,
        currency: str,
    ) -> bool:
        key = (server_id, kind, episode)
        if key in self.sent:
            return False
        self.sent.add(key)
        return True


@pytest.mark.parametrize(
    ("decision", "kind", "balance", "fragment"),
    [
        (PrepaidBalanceDecision.WARN, LowBalanceNotificationKind.WARN, 199_999, "200,000 تومان"),
        (PrepaidBalanceDecision.STOP, LowBalanceNotificationKind.STOP, 0, "خاموش شدن"),
        (PrepaidBalanceDecision.DELETE, LowBalanceNotificationKind.DELETE, 0, "یک روز"),
        (
            PrepaidBalanceDecision.RECOVERED,
            LowBalanceNotificationKind.RECOVERED,
            300_000,
            "300,000 تومان",
        ),
    ],
)
async def test_delivers_prepaid_notice_to_owner_once_per_episode(
    decision: PrepaidBalanceDecision,
    kind: LowBalanceNotificationKind,
    balance: int,
    fragment: str,
) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    users = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(telegram_user_id=138)))
    log = SentLog()
    service = LowBalanceNotifier(TelegramLowBalanceNotifier(bot, users), log)

    assert await service.notify(USER, SERVER, decision, balance, "IRT", EPISODE)
    assert not await service.notify(USER, SERVER, decision, balance, "IRT", EPISODE)
    users.get.assert_awaited_once_with(USER)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 138
    assert fragment in bot.send_message.await_args.kwargs["text"]
    assert (SERVER, kind, EPISODE) in log.sent
    assert await service.notify(USER, SERVER, decision, balance, "IRT", EPISODE + timedelta(days=1))
    assert bot.send_message.await_count == 2


async def test_telegram_failure_is_not_recorded_and_can_be_retried() -> None:
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("secret-token/138")))
    users = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(telegram_user_id=138)))
    log = SentLog()
    service = LowBalanceNotifier(TelegramLowBalanceNotifier(bot, users), log)

    with pytest.raises(TelegramNotificationDeliveryError) as error:
        await service.notify(USER, SERVER, PrepaidBalanceDecision.STOP, 0, "IRT", EPISODE)
    assert "secret-token" not in str(error.value)
    assert "138" not in str(error.value)
    assert log.sent == set()

    bot.send_message.side_effect = None
    assert await service.notify(USER, SERVER, PrepaidBalanceDecision.STOP, 0, "IRT", EPISODE)
    assert (SERVER, LowBalanceNotificationKind.STOP, EPISODE) in log.sent


async def test_missing_telegram_identity_is_not_recorded() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    users = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(telegram_user_id=None)))
    log = SentLog()
    service = LowBalanceNotifier(TelegramLowBalanceNotifier(bot, users), log)

    with pytest.raises(TelegramNotificationDeliveryError, match="No Telegram identity"):
        await service.notify(USER, SERVER, PrepaidBalanceDecision.WARN, 199_999, "IRT", EPISODE)
    assert not log.sent
    bot.send_message.assert_not_awaited()


async def test_already_recorded_notice_does_not_call_telegram() -> None:
    log = SentLog()
    log.sent.add((SERVER, LowBalanceNotificationKind.STOP, EPISODE))
    bot = SimpleNamespace(send_message=AsyncMock())
    users = SimpleNamespace(get=AsyncMock())
    service = LowBalanceNotifier(TelegramLowBalanceNotifier(bot, users), log)

    assert not await service.notify(USER, SERVER, PrepaidBalanceDecision.STOP, 0, "IRT", EPISODE)
    users.get.assert_not_awaited()
    bot.send_message.assert_not_awaited()


async def test_zero_decimal_irt_balance_is_not_divided_by_100() -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    users = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(telegram_user_id=138)))
    event = LowBalanceNotificationEvent(
        USER, SERVER, LowBalanceNotificationKind.WARN, 199_999, "IRT", EPISODE
    )

    await TelegramLowBalanceNotifier(bot, users).send(event)
    assert "199,999 تومان" in bot.send_message.await_args.kwargs["text"]
