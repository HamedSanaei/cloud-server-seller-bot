"""Provider account domain: a user's account with a cloud provider.

Creating a server requires the user to have an **active** account with the
target provider; the server row pins that account (``provider_account_id``)
so every server can be reconciled against the account that created it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderAccountStatus(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    DRAINING = "draining"
    DISABLED = "disabled"


class NoProviderAccountError(Exception):
    """Raised when a user has no active account for the requested provider."""


@dataclass(frozen=True, slots=True)
class ProviderAccount:
    """One user's account with one provider."""

    id: UUID
    user_id: UUID
    provider_key: str
    status: ProviderAccountStatus = ProviderAccountStatus.ACTIVE

    def __post_init__(self) -> None:
        if not self.provider_key or not self.provider_key.strip():
            raise ValueError("provider_key must not be empty")


class ProviderAccountRepository(Protocol):
    """Port for provider account persistence."""

    async def get_active(self, user_id: UUID, provider_key: str) -> ProviderAccount | None:
        """The user's active account with the provider, or None.

        Only ``ACTIVE`` accounts qualify for new server creation; degraded,
        draining and disabled accounts do not.
        """
        ...
