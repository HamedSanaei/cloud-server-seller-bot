"""Abuse module: map reported provider resources to responsible users (M10-006)."""

from cloud_platform.modules.abuse.domain import (
    AbuseCase,
    AbuseError,
    AbuseStatus,
    InvalidAbuseTransition,
    Ownership,
    ResourceNotManagedError,
    ResourceOwnershipResolver,
    ResourceRef,
    ResourceType,
)
from cloud_platform.modules.abuse.repository import (
    SqlAlchemyAbuseCaseRepository,
    SqlAlchemyOwnershipResolver,
)
from cloud_platform.modules.abuse.service import AbuseIntakeService

__all__ = [
    "AbuseCase",
    "AbuseError",
    "AbuseIntakeService",
    "AbuseStatus",
    "InvalidAbuseTransition",
    "Ownership",
    "ResourceNotManagedError",
    "ResourceOwnershipResolver",
    "ResourceRef",
    "ResourceType",
    "SqlAlchemyAbuseCaseRepository",
    "SqlAlchemyOwnershipResolver",
]
