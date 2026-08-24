"""Tests for low-balance notifications with deduplicated levels (M08-011).

Acceptance: deduplicated warning levels.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.billing.service import LowBalanceDecision
from cloud_platform.modules.notifications.domain import (
    LowBalanceNotificationEvent,
    LowBalanceNotificationKind,
    LowBalanceNotifier,
)
from cloud_platform.modules.notifications.repository import (
    SqlAlchemyLowBalanceNotificationLogRepository,
)

USER_ID = uuid4()
SERVER_ID = uuid4()
EP1 = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
EP2 = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


class FakeLog:
    """In-memory stand-in for the (server_id, kind, episode) unique log."""

    def __init__(self) -> None:
        self.records: list[tuple[UUID, UUID, LowBalanceNotificationKind, datetime]] = []

    async def record(
        self,
        user_id: UUID,
        server_id: UUID,
        kind: LowBalanceNotificationKind,
        episode: datetime,
        balance_minor: int,
        currency: str,
    ) -> bool:
        for _u, s, k, e in self.records:
            if s == server_id and k == kind and e == episode:
                return False
        self.records.append((user_id, server_id, kind, episode))
        return True


class RecordingPort:
    def __init__(self) -> None:
        self.events: list[LowBalanceNotificationEvent] = []

    async def send(self, event: LowBalanceNotificationEvent) -> None:
        self.events.append(event)


def _notifier(log: FakeLog, port: RecordingPort) -> LowBalanceNotifier:
    return LowBalanceNotifier(port, log)


class TestDeduplication:
    async def test_same_level_same_episode_notifies_once(self) -> None:
        log, port = FakeLog(), RecordingPort()
        notifier = _notifier(log, port)

        first = await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400, "EUR", EP1)
        second = await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400, "EUR", EP1)
        third = await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 350, "EUR", EP1)

        assert first is True
        assert second is False
        assert third is False
        assert len(port.events) == 1
        event = port.events[0]
        assert event.kind is LowBalanceNotificationKind.WARN
        assert event.balance_minor == 400  # the FIRST delivery's balance is recorded
        assert event.episode == EP1
        assert event.user_id == USER_ID
        assert event.server_id == SERVER_ID

    async def test_new_episode_notifies_again(self) -> None:
        # The watermark was cleared (recovered) and set again: a NEW episode
        # may re-notify the same level.
        log, port = FakeLog(), RecordingPort()
        notifier = _notifier(log, port)

        assert (
            await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400, "EUR", EP1)
            is True
        )
        assert (
            await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400, "EUR", EP2)
            is True
        )
        assert len(port.events) == 2

    async def test_different_levels_in_same_episode_are_independent(self) -> None:
        # warn -> auto_delete in one episode: both levels notify (different
        # kinds), but each level only once.
        log, port = FakeLog(), RecordingPort()
        notifier = _notifier(log, port)

        assert (
            await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400, "EUR", EP1)
            is True
        )
        assert (
            await notifier.notify(
                USER_ID, SERVER_ID, LowBalanceDecision.AUTO_DELETE, 300, "EUR", EP1
            )
            is True
        )
        assert (
            await notifier.notify(
                USER_ID, SERVER_ID, LowBalanceDecision.AUTO_DELETE, 300, "EUR", EP1
            )
            is False
        )
        assert [e.kind for e in port.events] == [
            LowBalanceNotificationKind.WARN,
            LowBalanceNotificationKind.AUTO_DELETE,
        ]

    async def test_recovered_closes_the_episode(self) -> None:
        log, port = FakeLog(), RecordingPort()
        notifier = _notifier(log, port)

        assert (
            await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400, "EUR", EP1)
            is True
        )
        assert (
            await notifier.notify(
                USER_ID, SERVER_ID, LowBalanceDecision.RECOVERED, 5000, "EUR", EP1
            )
            is True
        )
        assert [e.kind for e in port.events] == [
            LowBalanceNotificationKind.WARN,
            LowBalanceNotificationKind.RECOVERED,
        ]

    async def test_different_servers_are_independent(self) -> None:
        log, port = FakeLog(), RecordingPort()
        notifier = _notifier(log, port)
        other = uuid4()

        assert (
            await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, 400, "EUR", EP1)
            is True
        )
        assert (
            await notifier.notify(USER_ID, other, LowBalanceDecision.WARN, 400, "EUR", EP1) is True
        )
        assert len(port.events) == 2


class TestSilentLevels:
    @pytest.mark.parametrize("decision", [LowBalanceDecision.NONE, LowBalanceDecision.GRACE])
    async def test_none_and_grace_are_never_notified(self, decision: LowBalanceDecision) -> None:
        log, port = FakeLog(), RecordingPort()
        notifier = _notifier(log, port)

        delivered = await notifier.notify(USER_ID, SERVER_ID, decision, 400, "EUR", EP1)

        assert delivered is False
        assert port.events == []
        assert log.records == []  # silent levels never touch the dedup log


class TestEventRendering:
    async def test_render_uses_integer_minor_units(self) -> None:
        log, port = FakeLog(), RecordingPort()
        notifier = _notifier(log, port)
        await notifier.notify(USER_ID, SERVER_ID, LowBalanceDecision.WARN, -7, "EUR", EP1)
        (event,) = port.events
        text = event.render()
        assert "-0.07 EUR" in text
        assert "warn" in text
        assert EP1.isoformat() in text


class TestLoggingPort:
    async def test_logs_without_raising(self, caplog: pytest.LogCaptureFixture) -> None:
        from cloud_platform.modules.notifications.domain import _LoggingLowBalanceNotifier

        with caplog.at_level(logging.WARNING):
            await _LoggingLowBalanceNotifier().send(
                LowBalanceNotificationEvent(
                    user_id=USER_ID,
                    server_id=SERVER_ID,
                    kind=LowBalanceNotificationKind.AUTO_DELETE,
                    balance_minor=300,
                    currency="EUR",
                    episode=EP1,
                )
            )
        assert "auto_delete" in caplog.text
        assert "3.00 EUR" in caplog.text


# --------------------------------------------------------------------------
# Repository: the (server_id, kind, episode) constraint is the dedup guard
# --------------------------------------------------------------------------


class FakeSession:
    def __init__(self, fail_on_insert: bool) -> None:
        self._fail = fail_on_insert
        self.added: list[Any] = []
        self.rolled_back = False

    def add(self, model: Any) -> None:
        self.added.append(model)

    async def commit(self) -> None:
        if self._fail:
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    async def rollback(self) -> None:
        self.rolled_back = True


class _Ctx:
    def __init__(self, session: FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class TestSqlAlchemyLog:
    async def test_first_record_wins(self) -> None:
        session = FakeSession(fail_on_insert=False)
        repo = SqlAlchemyLowBalanceNotificationLogRepository(lambda: _Ctx(session))
        assert (
            await repo.record(USER_ID, SERVER_ID, LowBalanceNotificationKind.WARN, EP1, 400, "EUR")
            is True
        )
        assert len(session.added) == 1
        row = session.added[0]
        assert row.kind == "warn"
        assert row.episode == EP1
        assert row.balance_minor == 400
        assert row.currency == "EUR"

    async def test_duplicate_is_a_noop(self) -> None:
        session = FakeSession(fail_on_insert=True)
        repo = SqlAlchemyLowBalanceNotificationLogRepository(lambda: _Ctx(session))
        assert (
            await repo.record(USER_ID, SERVER_ID, LowBalanceNotificationKind.WARN, EP1, 400, "EUR")
            is False
        )
        assert session.rolled_back is True
