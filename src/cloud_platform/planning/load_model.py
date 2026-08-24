"""Realistic load model (M16-001).

Acceptance: user/order/job/provider assumptions documented.

This module ENCODES the platform's load assumptions as executable values
so capacity planning is checked, not guessed. Every number here has a
reason documented in ``docs/ops/load_model.md``; the tests assert the
model's internal consistency (mixes sum to 1, derived request rates match
hand computation, provider budgets keep the mandated headroom).

The model is pure math over integers/Decimals - no I/O, no clocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


class LoadModelError(ValueError):
    """The load model assumptions are inconsistent."""


@dataclass(frozen=True, slots=True)
class UserPopulation:
    """Who uses the platform.

    registered: total accounts ever created.
    daily_active: distinct users acting per 24h.
    peak_concurrent: simultaneous in-flight users at the daily peak
        (drives connection pools, not averages).
    """

    registered: int = 10_000
    daily_active: int = 1_500
    peak_concurrent: int = 120

    def __post_init__(self) -> None:
        if self.registered < self.daily_active:
            raise LoadModelError("daily_active cannot exceed registered")
        if self.daily_active < self.peak_concurrent:
            raise LoadModelError("peak_concurrent cannot exceed daily_active")


@dataclass(frozen=True, slots=True)
class OrderMix:
    """What users DO, as fractions of all user actions.

    Fractions must sum to exactly 1 (checked). Read-heavy by design: the
    bot/API surface is dominated by status reads, not mutations.
    """

    server_create: float = 0.02
    server_delete: float = 0.015
    power_action: float = 0.12
    rebuild: float = 0.005
    snapshot_op: float = 0.01
    ssh_key_op: float = 0.03
    wallet_read: float = 0.15
    server_read: float = 0.45
    catalog_read: float = 0.20

    def fractions(self) -> dict[str, float]:
        return {
            "server_create": self.server_create,
            "server_delete": self.server_delete,
            "power_action": self.power_action,
            "rebuild": self.rebuild,
            "snapshot_op": self.snapshot_op,
            "ssh_key_op": self.ssh_key_op,
            "wallet_read": self.wallet_read,
            "server_read": self.server_read,
            "catalog_read": self.catalog_read,
        }

    def __post_init__(self) -> None:
        total = sum(self.fractions().values())
        if abs(total - 1.0) > 1e-9:
            raise LoadModelError(f"order mix must sum to 1, got {total}")
        if any(v < 0 for v in self.fractions().values()):
            raise LoadModelError("order mix fractions must be non-negative")


@dataclass(frozen=True, slots=True)
class JobProfile:
    """Background jobs and their cadence.

    billing_tick_seconds: how often metering evaluates active servers -
        tied to the customer billing quantum (3600s default).
    reconcile_interval_seconds: provider reconciliation sweep.
    outbox_flush_seconds: transactional-outbox drain.
    worker_poll_seconds: operation worker claim poll.
    """

    billing_tick_seconds: int = 60
    reconcile_interval_seconds: int = 300
    outbox_flush_seconds: int = 5
    worker_poll_seconds: int = 2

    @property
    def billing_runs_per_hour(self) -> int:
        return 3600 // self.billing_tick_seconds

    @property
    def reconcile_runs_per_hour(self) -> int:
        return 3600 // self.reconcile_interval_seconds


@dataclass(frozen=True, slots=True)
class ProviderCapacity:
    """One provider adapter's budget.

    The provider's published API rate limit and the fraction of it the
    platform may use. HEADROOM_FACTOR is the minimum share of the limit
    that must stay free at modeled peak - external bursts (catalog syncs,
    reconciler sweeps after incidents) need room.
    """

    direction: Literal["read", "write"]
    requests_per_hour_limit: int
    max_utilized_fraction: float = 0.5

    def __post_init__(self) -> None:
        if self.requests_per_hour_limit <= 0:
            raise LoadModelError("requests_per_hour_limit must be positive")
        if not 0 < self.max_utilized_fraction <= 1:
            raise LoadModelError("max_utilized_fraction must be in (0, 1]")

    def rps_budget(self) -> float:
        """Requests/second the platform may issue to this provider."""
        return self.requests_per_hour_limit * self.max_utilized_fraction / 3600


#: Mandated safety headroom: at least this fraction of a provider's rate
#: limit stays unutilized at modeled peak.
HEADROOM_FACTOR = 0.5


@dataclass(frozen=True, slots=True)
class LoadModel:
    """The full assumption set, ready for derivation checks."""

    population: UserPopulation = field(default_factory=UserPopulation)
    mix: OrderMix = field(default_factory=OrderMix)
    jobs: JobProfile = field(default_factory=JobProfile)
    #: actions per active user per day across ALL kinds (reads dominate).
    actions_per_active_user_per_day: int = 40

    def actions_per_second_peak(self) -> float:
        """Peak user-action arrival rate.

        Peak concurrency does NOT spread evenly over the day: assume the
        whole day's actions can concentrate such that peak concurrent
        users each act once every ACTION_SPREAD_SECONDS.
        """
        return self.population.daily_active * self.actions_per_active_user_per_day / 86400

    def peak_actions_per_second(self) -> float:
        """Burst arrival rate when ALL peak-concurrent users act within one spread window."""
        return self.population.peak_concurrent / PEAK_ACTION_SPREAD_SECONDS

    def mutation_rps(self) -> dict[str, float]:
        base = self.actions_per_second_peak()
        mix = self.mix.fractions()
        mutating = ("server_create", "server_delete", "power_action", "rebuild", "snapshot_op")
        return {k: base * mix[k] for k in mutating}

    def provider_write_rps_required(self) -> float:
        """Provider-bound write RPS at BURST (creates/deletes/power/rebuild/snapshots).

        Power actions hit the provider; reads never do. SSH-key ops only on
        registration (already inside create flow), so they are excluded here.
        """
        mutations = self.mix.fractions()
        burst = self.peak_actions_per_second()
        mutating = ("server_create", "server_delete", "power_action", "rebuild", "snapshot_op")
        return sum(burst * mutations[k] for k in mutating)

    def check_provider_headroom(self, capacity: ProviderCapacity) -> None:
        required = self.provider_write_rps_required()
        budget = capacity.rps_budget()
        if required > budget:
            raise LoadModelError(
                f"modeled provider write load {required:.4f} rps exceeds "
                f"budget {budget:.4f} rps "
                f"({capacity.requests_per_hour_limit}/h x {capacity.max_utilized_fraction})"
            )


#: All peak-concurrent users act within this many seconds at burst time.
PEAK_ACTION_SPREAD_SECONDS = 60
