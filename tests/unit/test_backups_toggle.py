"""Tests for the backups toggle (M13-005).

Acceptance: **price impact confirmed before mutation.**

- The surcharge is an operator-declared basis-point rate card value
  (never a hardcoded provider percentage).
- ``preview`` computes the exact impact from the server's immutable
  price snapshot.
- ``set_enabled`` mutates ONLY when the caller re-presents the delta it
  confirmed; a moved price between preview and confirm REFUSES the
  mutation (stale confirmation) instead of billing an unseen amount.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.backups.domain import (
    BackupPricingError,
    BackupRateCard,
    BackupSettings,
    StaleConfirmationError,
    backup_monthly_minor,
)
from cloud_platform.modules.backups.service import BackupsToggleService


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> Any:
        self.events.append(event)
        return event


class InMemorySettings:
    def __init__(self) -> None:
        self.rows: dict[Any, BackupSettings] = {}

    async def get(self, server_id: Any) -> BackupSettings | None:
        return self.rows.get(server_id)

    async def upsert(self, settings: BackupSettings) -> BackupSettings:
        self.rows[settings.server_id] = settings
        return settings


class FakeSnapshots:
    """get_snapshot returns a selling_minor-bearing object or None."""

    def __init__(self, prices: dict[Any, int]) -> None:
        self.prices = prices

    async def get_snapshot(self, server_id: Any) -> Any:
        price = self.prices.get(server_id)
        if price is None:
            return None

        class _S:
            selling_minor = price

        return _S()


def make_service(
    prices: dict[Any, int],
    *,
    bps: int = 2000,
) -> tuple[BackupsToggleService, InMemorySettings, RecordingAudit]:
    settings_repo = InMemorySettings()
    audit = RecordingAudit()
    service = BackupsToggleService(
        settings_repo=settings_repo,
        price_snapshots=FakeSnapshots(prices),
        audit_repo=audit,
        rate_card_provider=lambda: BackupRateCard(surcharge_bps=bps),
    )
    return service, settings_repo, audit


class TestRateCard:
    def test_percent_helper(self) -> None:
        assert BackupRateCard.from_percent(20).surcharge_bps == 2000

    def test_bounds(self) -> None:
        with pytest.raises(BackupPricingError):
            BackupRateCard(surcharge_bps=-1)
        with pytest.raises(BackupPricingError):
            BackupRateCard(surcharge_bps=10_001)

    def test_zero_bps_is_valid_free_choice(self) -> None:
        assert backup_monthly_minor(BackupRateCard(0), 9_999) == 0


class TestMath:
    @pytest.mark.parametrize(
        ("bps", "base", "expected"),
        [
            (2000, 1000, 200),  # exactly 20%
            (2000, 999, 200),  # rounds HALF_UP to whole minor units
            (500, 1_000_000, 50_000),
            (0, 12345, 0),
            (10000, 777, 777),
        ],
    )
    def test_surcharge_matches_hand_computation(self, bps: int, base: int, expected: int) -> None:
        assert backup_monthly_minor(BackupRateCard(bps), base) == expected

    def test_negative_base_rejected(self) -> None:
        with pytest.raises(BackupPricingError):
            backup_monthly_minor(BackupRateCard(2000), -5)


class TestPreviewAndConfirm:
    SERVER = uuid4()

    async def test_unpriced_server_has_no_impact(self) -> None:
        service, _repo, _audit = make_service({})
        assert await service.preview(self.SERVER, enable=True) is None

    async def test_enable_preview_computes_delta(self) -> None:
        service, _repo, _audit = make_service({self.SERVER: 499}, bps=2000)
        impact = await service.preview(self.SERVER, enable=True)
        assert impact is not None
        assert impact.base_monthly_minor == 499
        assert impact.surcharge_monthly_minor == 100  # 20% of 499, half-up
        assert impact.total_monthly_minor == 599
        assert impact.delta_monthly_minor == 100

    async def test_disable_preview_negates(self) -> None:
        service, repo, _audit = make_service({self.SERVER: 499}, bps=2000)
        repo.rows[self.SERVER] = BackupSettings(
            server_id=self.SERVER, enabled=True, surcharge_bps_at_change=2000
        )
        impact = await service.preview(self.SERVER, enable=False)
        assert impact is not None
        assert impact.delta_monthly_minor == -100
        assert impact.total_monthly_minor == 499

    async def test_toggle_to_same_state_is_zero_delta(self) -> None:
        service, _repo, _audit = make_service({self.SERVER: 499})
        impact = await service.preview(self.SERVER, enable=False)  # default off
        assert impact is not None and impact.delta_monthly_minor == 0

    async def test_confirmed_mutation_persists_and_audits(self) -> None:
        service, _repo, audit = make_service({self.SERVER: 499})
        actor = uuid4()
        settings, impact = await service.set_enabled(
            actor_user_id=actor,
            server_id=self.SERVER,
            enable=True,
            confirmed_delta_minor=100,
            idempotency_key="bk-1",
        )
        assert settings.enabled is True
        assert settings.surcharge_bps_at_change == 2000
        assert settings.updated_by == actor
        assert impact.delta_monthly_minor == 100
        event = audit.events[-1]
        assert event.action == "backups.enabled"
        assert event.actor_type is ActorType.USER
        assert event.reason == ""

    async def test_wrong_confirmation_refuses_mutation(self) -> None:
        service, repo, audit = make_service({self.SERVER: 499})
        with pytest.raises(StaleConfirmationError):
            await service.set_enabled(
                actor_user_id=uuid4(),
                server_id=self.SERVER,
                enable=True,
                confirmed_delta_minor=50,  # user saw a different price
                idempotency_key="bk-2",
            )
        assert repo.rows == {}  # nothing mutated
        assert audit.events == []

    async def test_price_move_between_preview_and_confirm_refuses(self) -> None:
        prices: dict[Any, int] = {self.SERVER: 499}
        service, repo, _audit = make_service(prices)
        impact = await service.preview(self.SERVER, enable=True)
        assert impact is not None
        prices[self.SERVER] = 900  # snapshot changed after preview
        with pytest.raises(StaleConfirmationError):
            await service.set_enabled(
                actor_user_id=uuid4(),
                server_id=self.SERVER,
                enable=True,
                confirmed_delta_minor=impact.delta_monthly_minor,
                idempotency_key="bk-3",
            )
        assert repo.rows == {}

    async def test_missing_key_rejected_before_any_lookup(self) -> None:
        service, repo, audit = make_service({self.SERVER: 499})
        with pytest.raises(ValueError, match="idempotency_key"):
            await service.set_enabled(
                actor_user_id=uuid4(),
                server_id=self.SERVER,
                enable=True,
                confirmed_delta_minor=100,
                idempotency_key=" ",
            )
        assert repo.rows == {}
        assert audit.events == []
