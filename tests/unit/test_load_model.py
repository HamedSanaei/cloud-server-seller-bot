"""Tests for the load model (M16-001).

Acceptance: user/order/job/job/provider assumptions documented AND
internally consistent - mixes sum to 1, derivations match hand
computation, provider budgets keep the mandated headroom.
"""

from __future__ import annotations

import pytest

from cloud_platform.planning.load_model import (
    HEADROOM_FACTOR,
    PEAK_ACTION_SPREAD_SECONDS,
    JobProfile,
    LoadModel,
    LoadModelError,
    OrderMix,
    ProviderCapacity,
    UserPopulation,
)


class TestUserPopulation:
    def test_defaults_are_ordered(self) -> None:
        pop = UserPopulation()
        assert pop.registered >= pop.daily_active >= pop.peak_concurrent

    def test_active_cannot_exceed_registered(self) -> None:
        with pytest.raises(LoadModelError):
            UserPopulation(registered=100, daily_active=200, peak_concurrent=10)

    def test_peak_cannot_exceed_active(self) -> None:
        with pytest.raises(LoadModelError):
            UserPopulation(registered=1000, daily_active=50, peak_concurrent=60)


class TestOrderMix:
    def test_default_mix_sums_to_exactly_one(self) -> None:
        assert abs(sum(OrderMix().fractions().values()) - 1.0) < 1e-9

    def test_broken_mix_rejected(self) -> None:
        with pytest.raises(LoadModelError):
            OrderMix(server_read=0.9)  # now sums to > 1

    def test_negative_fractions_rejected(self) -> None:
        with pytest.raises(LoadModelError):
            OrderMix(power_action=-0.1)

    def test_reads_dominate_mutations(self) -> None:
        mix = OrderMix()
        reads = mix.wallet_read + mix.server_read + mix.catalog_read
        mutations = (
            mix.server_create + mix.server_delete + mix.power_action + mix.rebuild + mix.snapshot_op
        )
        assert reads > mutations


class TestJobProfile:
    def test_billing_tick_ties_to_hourly_quantum(self) -> None:
        jobs = JobProfile(billing_tick_seconds=60)
        assert jobs.billing_runs_per_hour == 60
        # hourly quantum metering still happens every tick; cadence is sane
        assert jobs.reconcile_runs_per_hour == 12


class TestProviderCapacity:
    def test_rps_budget_matches_hand_computation(self) -> None:
        cap = ProviderCapacity(
            direction="write", requests_per_hour_limit=3600, max_utilized_fraction=0.5
        )
        assert cap.rps_budget() == pytest.approx(0.5)

    def test_fraction_bounds_enforced(self) -> None:
        with pytest.raises(LoadModelError):
            ProviderCapacity("write", 3600, max_utilized_fraction=0)


class TestDerivations:
    def test_steady_arrival_matches_hand_computation(self) -> None:
        model = LoadModel()
        expected = 1500 * 40 / 86400
        assert model.actions_per_second_peak() == pytest.approx(expected, rel=1e-9)

    def test_burst_arrival_uses_spread_window(self) -> None:
        model = LoadModel()
        expected = 120 / PEAK_ACTION_SPREAD_SECONDS
        assert model.peak_actions_per_second() == pytest.approx(expected)

    def test_provider_write_load_is_mix_of_burst(self) -> None:
        model = LoadModel()
        mix = model.mix.fractions()
        manual = model.peak_actions_per_second() * (
            mix["server_create"]
            + mix["server_delete"]
            + mix["power_action"]
            + mix["rebuild"]
            + mix["snapshot_op"]
        )
        assert model.provider_write_rps_required() == pytest.approx(manual, rel=1e-9)

    def test_hetzner_budget_keeps_headroom(self) -> None:
        model = LoadModel()
        hetzner_writes = ProviderCapacity(
            direction="write", requests_per_hour_limit=3600, max_utilized_fraction=HEADROOM_FACTOR
        )
        model.check_provider_headroom(hetzner_writes)  # must not raise

    def test_headroom_violation_raises(self) -> None:
        tiny = LoadModel(
            population=UserPopulation(
                registered=10_000_000, daily_active=900_000, peak_concurrent=50_000
            )
        )
        cap = ProviderCapacity("write", 3600, max_utilized_fraction=0.5)
        with pytest.raises(LoadModelError):
            tiny.check_provider_headroom(cap)

    def test_documented_numbers_match_code(self) -> None:
        """docs/ops/load_model.md quotes these exact derived numbers."""
        model = LoadModel()
        assert round(model.actions_per_second_peak(), 2) == 0.69
        assert model.peak_actions_per_second() == 2.0
        assert round(model.provider_write_rps_required(), 2) == 0.34
