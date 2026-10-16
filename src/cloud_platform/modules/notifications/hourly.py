"""Aggregate customer hourly runway and durable six-hour Telegram reminders.

Forecasts use current IRT quotes and existing paid coverage, never mutate money.
A shared job lock serializes check/send/record across workers. As with the
existing Telegram notifier, a crash after send but before SQL commit may repeat
a delivery; transport failures are never recorded as successful deliveries.
"""

from __future__ import annotations

import heapq
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from cloud_platform.modules.billing.service import JobLock, PrepaidHourlyBillingService
from cloud_platform.modules.compute.domain import CloudServer, ServerRepository
from cloud_platform.modules.hourly.domain import operator_funded
from cloud_platform.modules.notifications.domain import (
    LowBalanceNotificationEvent,
    LowBalanceNotificationKind,
    LowBalanceNotificationLogRepository,
    LowBalanceNotifierPort,
)

logger = logging.getLogger(__name__)
WARNING_HOURS = 72
REMINDER_INTERVAL = timedelta(hours=6)


def forecast_exhaustion(
    balance: int, periods: list[tuple[UUID, int, datetime]], now: datetime
) -> datetime | None:
    """First unaffordable renewal in the next 72h, including prepaid time.

    Simulation respects staggered renewal boundaries instead of dividing by
    one server's rate. No floats, ledger writes, or guessed FX rates.
    """
    if balance < 0 or now.tzinfo is None:
        raise ValueError("nonnegative IRT balance and aware clock required")
    queue: list[tuple[datetime, UUID, int]] = []
    for server_id, cost, boundary in periods:
        if cost <= 0 or boundary.tzinfo is None:
            raise ValueError("positive hourly cost and aware paid boundary required")
        heapq.heappush(queue, (max(boundary, now), server_id, cost))
    horizon = now + timedelta(hours=WARNING_HOURS)
    while queue:
        boundary, server_id, cost = heapq.heappop(queue)
        if boundary >= horizon:
            return None
        if balance < cost:
            return boundary
        balance -= cost
        heapq.heappush(queue, (boundary + timedelta(hours=1), server_id, cost))
    return None


class HourlyRunwayWarningService:
    def __init__(
        self,
        *,
        servers: ServerRepository,
        billing: PrepaidHourlyBillingService,
        notifier: LowBalanceNotifierPort,
        log: LowBalanceNotificationLogRepository,
        lock: JobLock,
    ) -> None:
        self._servers, self._billing = servers, billing
        self._notifier, self._log, self._lock = notifier, log, lock

    async def run(self, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        async with self._lock.guard() as acquired:
            if not acquired:
                return 0
            return await self._run(moment)

    async def _run(self, now: datetime) -> int:
        groups: dict[UUID, list[CloudServer]] = defaultdict(list)
        for server in await self._servers.list_running():
            if server.is_prepaid_hourly_irt and not operator_funded(server):
                groups[server.user_id].append(server)
        sent = 0
        for user_id, servers in groups.items():
            try:
                last = await self._log.last_runway_sent(user_id)
                if last is not None and now - last < REMINDER_INTERVAL:
                    continue
                balance = await self._billing.spendable_balance(servers[0])
                periods = [
                    (
                        server.id,
                        await self._billing.next_hour_cost(server),
                        server.prepaid_paid_until or now,
                    )
                    for server in servers
                ]
                expires = forecast_exhaustion(balance, periods, now)
                if expires is None:
                    continue
                anchor = min(servers, key=lambda server: server.id)
                await self._notifier.send(
                    LowBalanceNotificationEvent(
                        user_id=user_id,
                        server_id=anchor.id,
                        kind=LowBalanceNotificationKind.RUNWAY,
                        balance_minor=balance,
                        currency="IRT",
                        episode=now,
                        at=now,
                        runway_hours=Decimal((expires - now) // timedelta(seconds=1))
                        / Decimal(3600),
                        active_servers=len(servers),
                    )
                )
                await self._log.record(
                    user_id, anchor.id, LowBalanceNotificationKind.RUNWAY, now, balance, "IRT"
                )
                sent += 1
            except Exception:
                # Never expose Telegram URLs/credentials in exception chains.
                logger.warning("hourly runway reminder unavailable for user %s", user_id)
        return sent
