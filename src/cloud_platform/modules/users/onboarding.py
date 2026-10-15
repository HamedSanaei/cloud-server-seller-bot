"""User onboarding service — /start flow for Telegram bots.

Handles both the new-user and returning-user paths:
- Returning user: look up by telegram_user_id, return existing User.
- New user: create User (with username/email) and Wallet, return aggregate.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from cloud_platform.modules.users.domain import User
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository
from cloud_platform.modules.wallet.domain import WalletRepository

logger = logging.getLogger(__name__)


class OnboardingConflict(ValueError):
    """Raised when username or email already belongs to another user."""


class OnboardingError(Exception):
    """Base error for onboarding failures."""


async def handle_start(
    user_repo: SqlAlchemyUserRepository,
    wallet_repo: WalletRepository,
    *,
    telegram_user_id: int,
    username: str,
    email: str | None = None,
) -> User:
    """Handle a Telegram /start command payload.

    Args:
        user_repo: Repository for user lookup/creation.
        wallet_repo: Repository for wallet creation.
        telegram_user_id: Unique Telegram user ID from the bot update.
        username: Telegram username (may be None for some bot patterns).
        email: User's email, if shared.

    Returns:
        The resolved or newly-created User aggregate.
    """
    # Keep the public service signature, but the user, pending grant, wallet
    # and immutable audit now commit atomically in the identity repository.
    del wallet_repo
    return await user_repo.onboard_telegram(
        telegram_user_id,
        username,
        email or _generate_email(username or f"telegram_{telegram_user_id}"),
    )


async def ensure_telegram_link(
    user_repo: SqlAlchemyUserRepository, user: User, telegram_user_id: int
) -> User:
    """Link a user's Telegram identity when it was not recorded at creation.

    Returns the updated user. No-op when already linked.
    """
    if user.telegram_user_id == telegram_user_id or user.id is None:
        return user
    return await user_repo.update_telegram_user_id(user.id, telegram_user_id)


def _generate_email(username: str) -> str:
    """Generate a deterministic email from a username."""
    short = uuid4().hex[:6]
    return f"{username}_{short}@t.me"
