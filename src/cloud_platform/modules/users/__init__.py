"""User management module: aggregate, port, and SQLAlchemy adapter."""

from cloud_platform.modules.users.domain import (
    InvalidUserTransition,
    Permission,
    PermissionChecker,
    PermissionDeniedError,
    Role,
    TermsAcceptanceRequiredError,
    TermsVersion,
    TermsVersionRepository,
    User,
    UserNotFound,
    UserRepository,
    UserStatus,
)
from cloud_platform.modules.users.onboarding import (
    OnboardingConflict,
    OnboardingError,
    handle_start,
)
from cloud_platform.modules.users.repository import (
    SqlAlchemyTermsVersionRepository,
    SqlAlchemyUserRepository,
)
from cloud_platform.modules.users.terms import TermsService

__all__ = [
    "InvalidUserTransition",
    "OnboardingConflict",
    "OnboardingError",
    "Permission",
    "PermissionChecker",
    "PermissionDeniedError",
    "Role",
    "SqlAlchemyTermsVersionRepository",
    "SqlAlchemyUserRepository",
    "TermsAcceptanceRequiredError",
    "TermsService",
    "TermsVersion",
    "TermsVersionRepository",
    "User",
    "UserNotFound",
    "UserRepository",
    "UserStatus",
    "handle_start",
]
