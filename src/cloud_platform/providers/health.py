"""Provider account health and status management.

This module defines the account status lifecycle, health check interfaces,
and utilities for managing provider account availability.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol


class AccountStatus(StrEnum):
    """Provider account status states."""

    ACTIVE = "active"  # Fully operational, accepting new workloads
    DRAINING = "draining"  # No new workloads, finishing existing
    DISABLED = "disabled"  # Manually disabled, no operations
    DEGRADED = "degraded"  # Operational but with issues (rate limits, errors)


# Valid status transitions
_STATUS_TRANSITIONS: dict[AccountStatus, frozenset[AccountStatus]] = {
    AccountStatus.ACTIVE: frozenset(
        {AccountStatus.DRAINING, AccountStatus.DISABLED, AccountStatus.DEGRADED}
    ),
    AccountStatus.DRAINING: frozenset({AccountStatus.DISABLED, AccountStatus.DEGRADED}),
    AccountStatus.DISABLED: frozenset({AccountStatus.ACTIVE, AccountStatus.DRAINING}),
    AccountStatus.DEGRADED: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.DRAINING, AccountStatus.DISABLED}
    ),
}


class AccountHealth(StrEnum):
    """Health check result states."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ProviderAccountHealthCheck(Protocol):
    """Protocol for provider-specific health checks."""

    async def check(self, credentials: dict[str, str]) -> AccountHealth:
        """Check provider account health using credentials.

        Args:
            credentials: Decrypted provider credentials

        Returns:
            Health status of the account
        """
        ...


def can_transition(current: AccountStatus, target: AccountStatus) -> bool:
    """Check if a status transition is valid.

    Args:
        current: Current account status
        target: Desired target status

    Returns:
        True if transition is allowed
    """
    return target in _STATUS_TRANSITIONS.get(current, frozenset())


def transition_status(current: AccountStatus, target: AccountStatus) -> AccountStatus:
    """Perform a status transition with validation.

    Args:
        current: Current account status
        target: Desired target status

    Returns:
        The new status (same as target if valid)

    Raises:
        ValueError: If transition is not allowed
    """
    if not can_transition(current, target):
        raise ValueError(f"Invalid status transition: {current.value} -> {target.value}")
    return target


def is_operational(status: AccountStatus) -> bool:
    """Check if an account status allows new workloads.

    Args:
        status: Account status to check

    Returns:
        True if account can accept new workloads
    """
    return status in (AccountStatus.ACTIVE, AccountStatus.DEGRADED)


def is_terminal(status: AccountStatus) -> bool:
    """Check if an account status is terminal (no workloads possible).

    Args:
        status: Account status to check

    Returns:
        True if no operations should run
    """
    return status in (AccountStatus.DISABLED,)


async def evaluate_account_health(
    health_check: ProviderAccountHealthCheck,
    credentials: dict[str, str],
    consecutive_failures: int,
    failure_threshold: int = 3,
) -> AccountStatus:
    """Evaluate account status based on health check results.

    Args:
        health_check: Provider-specific health check implementation
        credentials: Decrypted credentials for the provider
        consecutive_failures: Number of consecutive health check failures
        failure_threshold: Number of failures before marking degraded

    Returns:
        Recommended account status
    """
    try:
        health = await health_check.check(credentials)
    except Exception:
        health = AccountHealth.UNKNOWN

    if health == AccountHealth.HEALTHY:
        return AccountStatus.ACTIVE
    elif health == AccountHealth.DEGRADED:
        return AccountStatus.DEGRADED
    elif health == AccountHealth.UNHEALTHY or consecutive_failures >= failure_threshold:
        return AccountStatus.DEGRADED
    else:
        return AccountStatus.ACTIVE
