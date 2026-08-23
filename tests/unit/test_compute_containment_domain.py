"""Tests for the containment transitions in the compute domain (M10-007)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from cloud_platform.modules.compute.domain import (
    CONTAINABLE_STATES,
    CloudServer,
)
from cloud_platform.modules.compute.domain import (
    ServerLifecycleState as State,
)

USER_ID = uuid4()
ACCOUNT_ID = uuid4()


def _server(state: State, **kwargs: object) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=USER_ID,
        provider_key="hetzner",
        provider_account_id=ACCOUNT_ID,
        state=state,
        **kwargs,  # type: ignore[arg-type]
    )


class TestContainableStates:
    def test_every_containable_state_reaches_manual_review(self) -> None:
        for state in CONTAINABLE_STATES:
            server = _server(state)
            prior = server.contain()
            assert prior is state
            assert server.state is State.MANUAL_REVIEW
            assert server.contained_from is state

    def test_non_containable_states_rejected(self) -> None:
        for state in (State.REQUESTED, State.DELETED, State.MANUAL_REVIEW):
            server = _server(state)
            if state is State.MANUAL_REVIEW:
                # already contained -> idempotent path, not an error
                with pytest.raises(ValueError):
                    server.contain()  # no contained_from recorded
                continue
            assert server.is_containable is False
            with pytest.raises(ValueError):
                server.contain()

    def test_running_and_stopped_are_now_containable(self) -> None:
        assert State.RUNNING in CONTAINABLE_STATES
        assert State.STOPPED in CONTAINABLE_STATES

    def test_contain_records_prior_state(self) -> None:
        server = _server(State.RUNNING)
        server.contain()
        assert server.contained_from is State.RUNNING
        assert server.state is State.MANUAL_REVIEW


class TestContainIdempotency:
    def test_double_contain_returns_original_state(self) -> None:
        server = _server(State.STOPPED)
        first = server.contain()
        second = server.contain()
        assert first is State.STOPPED
        assert second is State.STOPPED
        assert server.contained_from is State.STOPPED

    def test_contain_without_prior_state_raises(self) -> None:
        server = _server(State.MANUAL_REVIEW)
        assert server.contained_from is None
        with pytest.raises(ValueError, match="no recorded prior state"):
            server.contain()


class TestRelease:
    @pytest.mark.parametrize(
        "prior",
        sorted(CONTAINABLE_STATES, key=lambda s: s.value),
    )
    def test_release_returns_to_exactly_prior_state(self, prior: State) -> None:
        server = _server(prior)
        server.contain()
        target = server.release()
        assert target is prior
        assert server.state is prior
        assert server.contained_from is None

    def test_release_when_not_contained_raises(self) -> None:
        server = _server(State.RUNNING)
        with pytest.raises(ValueError, match="cannot release"):
            server.release()

    def test_release_without_prior_state_raises(self) -> None:
        server = _server(State.MANUAL_REVIEW)
        with pytest.raises(ValueError, match="no recorded prior state"):
            server.release()

    def test_recontain_after_release_works(self) -> None:
        server = _server(State.RUNNING)
        server.contain()
        server.release()
        assert server.state is State.RUNNING
        server.contain()
        assert server.state is State.MANUAL_REVIEW
        assert server.contained_from is State.RUNNING


class TestTransitionMap:
    def test_manual_review_reaches_every_containable_state(self) -> None:
        """Release targets must be valid transitions out of MANUAL_REVIEW."""
        from cloud_platform.modules.compute.domain import _ALLOWED

        for state in CONTAINABLE_STATES:
            assert state in _ALLOWED[State.MANUAL_REVIEW]

    def test_deleted_remains_terminal(self) -> None:
        from cloud_platform.modules.compute.domain import _ALLOWED

        assert _ALLOWED[State.DELETED] == frozenset()

    def test_existing_transitions_unchanged(self) -> None:
        server = _server(State.REQUESTED)
        server.transition_to(State.PROVISIONING)
        server.transition_to(State.RUNNING)
        assert server.state is State.RUNNING
