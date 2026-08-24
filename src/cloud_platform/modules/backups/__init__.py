"""Server backups toggle (M13-005).

Acceptance: price impact confirmed before mutation.

Enabling backups is a PAID change to a server. The flow is two-phase:

1. ``preview(server_id)`` computes the :class:`BackupPriceImpact` from the
   server's immutable price snapshot and the operator-declared surcharge.
2. ``set_enabled(...)`` performs the mutation ONLY when the caller passes
   the impact it confirmed (the delta the user saw). If the underlying
   price moved between preview and confirm, the mutation is REFUSED
   (stale confirmation) instead of silently billing a different amount.

The surcharge is an EXPLICIT OPERATOR INPUT in basis points of the
server's monthly price - provider percentages are never hardcoded.
"""

from .domain import (
    BackupPriceImpact,
    BackupPricingError,
    BackupRateCard,
    BackupSettings,
    StaleConfirmationError,
    backup_monthly_minor,
)
from .repository import SqlAlchemyBackupSettingsRepository
from .service import BackupsToggleService

__all__ = [
    "BackupPriceImpact",
    "BackupPricingError",
    "BackupRateCard",
    "BackupSettings",
    "BackupsToggleService",
    "SqlAlchemyBackupSettingsRepository",
    "StaleConfirmationError",
    "backup_monthly_minor",
]
