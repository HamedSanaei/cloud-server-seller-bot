"""Provider-neutral commercial renewal policy (`[commerce.*]`, §19-§30).

This is the OPERATOR's business policy for collecting money from customers. It
is deliberately separate from the provider's infrastructure contract:

* the provider renews ITS contract with us at its own billing cycle;
* this policy decides when WE ask the customer's wallet for the next period,
  how we warn them, how long we let them pay, and what we do when they don't.

Nothing in here can call a provider API. Suspension is a *commercial* decision
whose only optional infrastructure effect is an explicit, configurable, audited
``stop`` — and ``stop`` is never a termination (see §30-§31).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["CommerceRenewalPolicy"]


@dataclass(frozen=True, slots=True)
class CommerceRenewalPolicy:
    """The configured collection/warning/grace/suspension policy."""

    enabled: bool = True
    #: Earliest moment (hours before expiry) an automatic charge may be tried.
    charge_before_expiry_hours: int = 72
    #: Customer warning thresholds, in hours before expiry, most distant first.
    warning_before_expiry_hours: tuple[int, ...] = (168, 72, 24)
    #: How long an expired, unpaid service stays payable before suspension.
    grace_period_hours: int = 48
    #: Automatic renewal applied to NEWLY provisioned services only.
    auto_renew_default: bool = True
    #: Whether commercial suspension may stop the provider server. Default OFF:
    #: suspension must never silently take a customer's server down.
    stop_server_after_grace: bool = False

    def __post_init__(self) -> None:
        if self.charge_before_expiry_hours < 0:
            raise ValueError("charge_before_expiry_hours must be >= 0")
        if self.grace_period_hours < 0:
            raise ValueError("grace_period_hours must be >= 0")
        if any(hours < 0 for hours in self.warning_before_expiry_hours):
            raise ValueError("warning thresholds must be >= 0")

    @classmethod
    def from_settings(cls, settings: Any) -> CommerceRenewalPolicy:
        """Build the policy from ``Settings`` (``[commerce.renewal]``)."""
        raw = getattr(settings, "commerce_renewal_warning_before_expiry_hours", "")
        hours: list[int] = []
        for part in str(raw or "").replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                value = int(part)
            except ValueError:
                continue
            if value >= 0:
                hours.append(value)
        return cls(
            enabled=bool(getattr(settings, "commerce_renewal_enabled", True)),
            charge_before_expiry_hours=int(
                getattr(settings, "commerce_renewal_charge_before_expiry_hours", 72)
            ),
            warning_before_expiry_hours=tuple(sorted(set(hours), reverse=True)) or (168, 72, 24),
            grace_period_hours=int(getattr(settings, "commerce_renewal_grace_period_hours", 48)),
            auto_renew_default=bool(getattr(settings, "commerce_renewal_auto_renew_default", True)),
            stop_server_after_grace=bool(
                getattr(settings, "commerce_suspension_stop_server_after_grace", False)
            ),
        )

    def warnings_due(self, hours_remaining: float) -> tuple[int, ...]:
        """The thresholds ``hours_remaining`` has reached, most distant first.

        A threshold counts as reached once the service is within it, so the
        checker can send every crossed warning exactly once per period (the
        dedup log is what makes it exactly-once).
        """
        return tuple(
            hours for hours in self.warning_before_expiry_hours if hours_remaining <= hours
        )

    def charge_window_open(self, hours_remaining: float) -> bool:
        """Whether the automatic charge window has opened for this period."""
        return hours_remaining <= self.charge_before_expiry_hours
