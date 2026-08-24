"""Backups toggle service (M13-005).

Acceptance: price impact confirmed before mutation.

``preview`` computes the impact from the server's immutable price
snapshot; ``set_enabled`` mutates ONLY when the caller re-presents the
delta it confirmed. A moved underlying price between preview and confirm
raises :class:`StaleConfirmationError` - the user is never billed an
amount they did not see.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from cloud_platform.modules.audit.domain import ActorType
from cloud_platform.modules.audit.service import AuditTrail

from .domain import (
    BackupPriceImpact,
    BackupRateCard,
    BackupSettings,
    StaleConfirmationError,
    backup_monthly_minor,
)


class PriceSnapshotPort(Protocol):
    """Reads a server's immutable monthly price (M06-002 snapshots)."""

    async def get_snapshot(self, server_id: UUID) -> Any: ...


class BackupSettingsRepository(Protocol):
    async def get(self, server_id: UUID) -> BackupSettings | None: ...
    async def upsert(self, settings: BackupSettings) -> BackupSettings: ...


class BackupsToggleService:
    """Two-phase backups toggle with confirmed price impact."""

    resource_type = "server_backup_settings"

    def __init__(
        self,
        *,
        settings_repo: BackupSettingsRepository,
        price_snapshots: PriceSnapshotPort,
        audit_repo: Any,
        rate_card_provider: Callable[[], BackupRateCard],
    ) -> None:
        self._settings = settings_repo
        self._snapshots = price_snapshots
        self._audit = AuditTrail(audit_repo)
        self._card = rate_card_provider

    async def current_state(self, server_id: UUID) -> BackupSettings:
        settings = await self._settings.get(server_id)
        return settings or BackupSettings(
            server_id=server_id, enabled=False, surcharge_bps_at_change=0
        )

    async def preview(self, server_id: UUID, *, enable: bool) -> BackupPriceImpact | None:
        """The impact of switching to ``enable``; None when unpriced."""
        snapshot = await self._snapshots.get_snapshot(server_id)
        if snapshot is None:
            return None
        base = int(snapshot.selling_minor)
        surcharge = backup_monthly_minor(self._card(), base)
        currently_on = (await self.current_state(server_id)).enabled
        if enable == currently_on:
            delta = 0  # no change
        elif enable:
            delta = surcharge
        else:
            delta = -surcharge
        total = base + surcharge if enable else base
        return BackupPriceImpact(
            server_id=server_id,
            enabled=enable,
            base_monthly_minor=base,
            surcharge_monthly_minor=surcharge,
            total_monthly_minor=total,
            delta_monthly_minor=delta,
        )

    async def set_enabled(
        self,
        *,
        actor_user_id: UUID,
        server_id: UUID,
        enable: bool,
        confirmed_delta_minor: int,
        idempotency_key: str,
    ) -> tuple[BackupSettings, BackupPriceImpact]:
        """Apply the toggle only if the price impact matches the confirmation.

        ``confirmed_delta_minor`` is what the user SAW and accepted in the
        preview. If the recomputed impact differs (price snapshot changed,
        rate card changed), the mutation is refused.
        """
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required for backups mutations")
        impact = await self.preview(server_id, enable=enable)
        if impact is None:
            raise StaleConfirmationError("server has no price snapshot to compute impact from")
        if impact.delta_monthly_minor != confirmed_delta_minor:
            raise StaleConfirmationError(
                f"confirmed delta {confirmed_delta_minor} does not match "
                f"current impact {impact.delta_monthly_minor}; show the new price again"
            )
        card = self._card()
        settings = await self._settings.upsert(
            BackupSettings(
                server_id=server_id,
                enabled=enable,
                surcharge_bps_at_change=card.surcharge_bps,
                updated_by=actor_user_id,
                updated_at=datetime.now(UTC),
            )
        )
        await self._audit.record_mutation(
            actor_type=ActorType.USER,
            actor_id=actor_user_id,
            action="backups.enabled" if enable else "backups.disabled",
            resource_type=self.resource_type,
            resource_id=str(server_id),
            metadata={
                "delta_monthly_minor": str(impact.delta_monthly_minor),
                "surcharge_bps": str(card.surcharge_bps),
                "idempotency_key": idempotency_key,
            },
        )
        return settings, impact
