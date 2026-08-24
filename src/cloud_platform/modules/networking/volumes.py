"""Volume domain values (M13-009).

A volume is a user-owned block-storage disk, optionally attached to one
of the user's servers. Sizes are whole GB (provider API convention);
the binding is reconciled against the provider (attach/detach/delete).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

_MIN_SIZE_GB = 10
_MAX_SIZE_GB = 10_240


class VolumeError(Exception):
    """Base class for volume errors."""


class VolumeNotFoundError(VolumeError):
    """Another user's (or unknown) volume reads as missing."""


class VolumeLimitError(VolumeError):
    """The user hit their volume quota."""


class VolumeSizeError(VolumeError, ValueError):
    """Invalid volume size."""


@dataclass(frozen=True, slots=True)
class Volume:
    """One provider block-storage volume owned by a platform user."""

    user_id: UUID
    provider_account_id: UUID
    provider_key: str
    provider_volume_id: str
    name: str
    size_gb: int
    location_id: str | None = None
    server_id: UUID | None = None  # current attachment, if any
    id: UUID | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not (_MIN_SIZE_GB <= self.size_gb <= _MAX_SIZE_GB):
            raise VolumeSizeError(
                f"volume size must be between {_MIN_SIZE_GB} and {_MAX_SIZE_GB} GB"
            )
        if not self.provider_volume_id or not self.provider_volume_id.strip():
            raise VolumeError("provider_volume_id must not be empty")

    @property
    def is_attached(self) -> bool:
        return self.server_id is not None
