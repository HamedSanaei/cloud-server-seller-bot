"""Balance notices enqueue once; storage failures cannot suppress the next attempt."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from cloud_platform.modules.billing.service import LowBalanceDecision, PrepaidBalanceDecision
from cloud_platform.modules.notifications.customer import LowBalanceOutboxNotifier
from cloud_platform.modules.notifications.domain import (
    LowBalanceNotificationEvent,
    LowBalanceNotificationKind,
    LowBalanceNotifier,
    render_low_balance_notice,
)

USER, SERVER = uuid4(), uuid4()
EPISODE = datetime(2026, 9, 27, tzinfo=UTC)


class SentLog:
    def __init__(self):
        self.sent = set()
        self.fail_record = False

    async def was_sent(self, server_id, kind, episode):
        return (server_id, kind, episode) in self.sent

    async def record(self, user_id, server_id, kind, episode, balance, currency):
        if self.fail_record:
            raise RuntimeError("notification log unavailable")
        key = (server_id, kind, episode)
        if key in self.sent:
            return False
        self.sent.add(key)
        return True


class Outbox:
    def __init__(self):
        self.rows = {}
        self.unavailable = False

    async def enqueue(self, *, event_key, event_type, payload):
        if self.unavailable:
            raise RuntimeError("outbox unavailable")
        if event_key in self.rows:
            return False
        self.rows[event_key] = {"event_type": event_type, "payload": payload}
        return True


@pytest.mark.parametrize(
    "decision",
    [
        PrepaidBalanceDecision.WARN,
        PrepaidBalanceDecision.STOP,
        PrepaidBalanceDecision.DELETE,
        PrepaidBalanceDecision.RECOVERED,
        LowBalanceDecision.AUTO_DELETE,
    ],
)
async def test_balance_episode_publishes_once_and_a_new_episode_can_notify(decision):
    outbox, log = Outbox(), SentLog()
    service = LowBalanceNotifier(LowBalanceOutboxNotifier(outbox), log)
    assert await service.notify(USER, SERVER, decision, 199_999, "IRT", EPISODE)
    assert not await service.notify(USER, SERVER, decision, 199_999, "IRT", EPISODE)
    first_keys = set(outbox.rows)
    assert len(first_keys) == 1
    assert await service.notify(USER, SERVER, decision, 199_999, "IRT", EPISODE + timedelta(days=1))
    assert len(set(outbox.rows) - first_keys) == 1


async def test_failed_enqueue_is_not_recorded_as_a_successful_notice():
    outbox, log = Outbox(), SentLog()
    outbox.unavailable = True
    service = LowBalanceNotifier(LowBalanceOutboxNotifier(outbox), log)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await service.notify(USER, SERVER, PrepaidBalanceDecision.STOP, 0, "IRT", EPISODE)
    assert not log.sent
    outbox.unavailable = False
    assert await service.notify(USER, SERVER, PrepaidBalanceDecision.STOP, 0, "IRT", EPISODE)


async def test_commit_window_recovers_without_a_second_customer_event():
    outbox, log = Outbox(), SentLog()
    service = LowBalanceNotifier(LowBalanceOutboxNotifier(outbox), log)
    log.fail_record = True
    with pytest.raises(RuntimeError, match="notification log unavailable"):
        await service.notify(USER, SERVER, PrepaidBalanceDecision.STOP, 0, "IRT", EPISODE)
    original_key = next(iter(outbox.rows))
    log.fail_record = False
    assert await service.notify(USER, SERVER, PrepaidBalanceDecision.STOP, 0, "IRT", EPISODE)
    assert set(outbox.rows) == {original_key}


def test_zero_decimal_toman_is_not_divided_by_one_hundred():
    event = LowBalanceNotificationEvent(
        USER, SERVER, LowBalanceNotificationKind.WARN, 199_999, "IRT", EPISODE
    )
    assert "199,999 تومان" in render_low_balance_notice(event)
    with pytest.raises(ValueError, match="require IRT"):
        render_low_balance_notice(
            LowBalanceNotificationEvent(
                USER, SERVER, LowBalanceNotificationKind.WARN, 199_999, "USD", EPISODE
            )
        )
