"""Admin console module: re-authorized application facades for the Telegram UI."""

from cloud_platform.modules.admin_console.users import (
    AdminServerSummary,
    AdminUserProfile,
    AdminUserRepository,
    AdminUserSearchPage,
    AdminUserService,
)
from cloud_platform.modules.admin_console.wallet import (
    AdminWalletAdjustmentService,
    AdminWalletConfirmation,
)

__all__ = [
    "AdminServerSummary",
    "AdminUserProfile",
    "AdminUserRepository",
    "AdminUserSearchPage",
    "AdminUserService",
    "AdminWalletAdjustmentService",
    "AdminWalletConfirmation",
]
