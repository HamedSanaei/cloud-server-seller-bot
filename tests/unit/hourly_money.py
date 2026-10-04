"""Reservation ports for tests concerned with provider lifecycle behavior."""

from unittest.mock import AsyncMock
from uuid import uuid4

from cloud_platform.modules.wallet.domain import Hold, HoldStatus


class Reservations:
    def __init__(self):
        self.holds = {}

    async def create_hold(self, wallet_id, amount, currency, key):
        existing = self.holds.get(key)
        if existing is not None:
            assert existing.amount == amount and existing.currency == currency
            return existing
        hold = Hold(wallet_id, amount, currency, key, id=uuid4())
        self.holds[key] = hold
        return hold

    async def get_by_idempotency(self, wallet_id, key):
        return self.holds.get(key)

    async def release_hold(self, wallet_id, hold_id, key):
        hold = self.holds[key]
        assert hold.id == hold_id and hold.status is not HoldStatus.CAPTURED
        if hold.status is HoldStatus.CREATED:
            hold.release()
        return hold


def hourly_money():
    reservations = Reservations()
    return {
        "hold_repo": reservations,
        "hold_service": reservations,
        "prepay_server": AsyncMock(),
    }
