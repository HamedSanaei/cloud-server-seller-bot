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
    # --- Returning user path ---
    existing = await user_repo.get_by_telegram_user_id(telegram_user_id)
    if existing is not None:
        logger.info("Returning user %s logged in via Telegram", existing.username)
        return existing

    # --- New user path ---
    resolved_email = email or _generate_email(username)
    try:
        user = await user_repo.create(
            User(username=username, email=resolved_email, telegram_user_id=telegram_user_id)
        )
    except Exception as exc:
        # Handle unique constraint violations (username/email already taken)
        if isinstance(exc, (ValueError, OnboardingConflict)):
            raise
        # sqlalchemy IntegrityError → parse into domain error
        raise OnboardingError(f"failed to create user: {exc}") from exc

    # Create wallet for new user
    assert user.id is not None
    await wallet_repo.get_or_create(user.id, currency="EUR")
    logger.info("Created user %s with wallet", username)
    return user


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
