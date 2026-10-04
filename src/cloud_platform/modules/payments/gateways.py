"""Durable, administrator-controlled policy for starting payment orders.

Missing overrides preserve configured gateways' enabled default. Adapters stay
registered when switched off so existing invoices remain reconcilable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from cloud_platform.modules.users.domain import PermissionDeniedError, Role, UserStatus


class GatewaySettingsRepository(Protocol):
    async def get(self, key: str) -> bool | None: ...

    async def set_enabled(self, key: str, enabled: bool, actor_id: UUID) -> None: ...

    async def list(self) -> dict[str, bool]: ...


@dataclass(frozen=True, slots=True)
class GatewayAvailability:
    key: str
    enabled: bool


class GatewayManagementService:
    def __init__(
        self, repository: GatewaySettingsRepository, configured_keys: Iterable[str]
    ) -> None:
        self._repository = repository
        self._keys = tuple(dict.fromkeys(configured_keys))

    @staticmethod
    def _require_admin(admin: object) -> UUID:
        actor_id = getattr(admin, "id", None)
        if (
            not isinstance(actor_id, UUID)
            or getattr(admin, "role", None) != Role.ADMIN
            or getattr(admin, "status", None) != UserStatus.ACTIVE
        ):
            raise PermissionDeniedError("active administrator required")
        return actor_id

    async def list(self, admin: object) -> list[GatewayAvailability]:
        self._require_admin(admin)
        overrides = await self._repository.list()
        return [GatewayAvailability(key, overrides.get(key, True)) for key in self._keys]

    async def set_enabled(self, admin: object, key: str, enabled: bool) -> None:
        actor_id = self._require_admin(admin)
        if key not in self._keys:
            raise ValueError("gateway is not configured")
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        await self._repository.set_enabled(key, enabled, actor_id)
