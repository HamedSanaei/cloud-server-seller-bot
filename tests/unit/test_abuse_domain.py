"""Tests for the abuse-case domain: state machine and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cloud_platform.modules.abuse.domain import (
    AbuseCase,
    AbuseStatus,
    InvalidAbuseTransition,
    ResourceRef,
    ResourceType,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _ref(**overrides: object) -> ResourceRef:
    defaults: dict[str, object] = {
        "provider_key": "hetzner",
        "resource_type": ResourceType.PROVIDER_SERVER,
        "resource_id": "srv-42",
    }
    defaults.update(overrides)
    return ResourceRef(**defaults)  # type: ignore[arg-type]


def _case(**overrides: object) -> AbuseCase:
    defaults: dict[str, object] = {
        "resource": _ref(),
        "user_id": uuid4(),
        "reason": "port scan observed",
        "reporter": "hetzner-abuse",
    }
    defaults.update(overrides)
    return AbuseCase(**defaults)  # type: ignore[arg-type]


class TestResourceRef:
    def test_empty_provider_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="provider_key"):
            _ref(provider_key="  ")

    def test_empty_resource_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="resource_id"):
            _ref(resource_id="")

    def test_resource_types(self) -> None:
        assert ResourceType.PROVIDER_SERVER.value == "provider_server"
        assert ResourceType.IPV4.value == "ipv4"
        assert ResourceType.IPV6.value == "ipv6"


class TestCaseValidation:
    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValueError, match="reason"):
            _case(reason="")

    def test_empty_reporter_rejected(self) -> None:
        with pytest.raises(ValueError, match="reporter"):
            _case(reporter="  ")

    def test_defaults_to_open(self) -> None:
        assert _case().status is AbuseStatus.OPEN
        assert _case().server_id is None


class TestStateMachine:
    def test_open_to_investigating(self) -> None:
        moved = _case().transition(AbuseStatus.INVESTIGATING, at=NOW)
        assert moved.status is AbuseStatus.INVESTIGATING
        assert moved.resolved_at is None

    def test_open_to_dismissed(self) -> None:
        moved = _case().transition(AbuseStatus.DISMISSED, at=NOW)
        assert moved.status is AbuseStatus.DISMISSED
        assert moved.resolved_at == NOW

    def test_investigating_to_resolved(self) -> None:
        case = _case().transition(AbuseStatus.INVESTIGATING, at=NOW)
        moved = case.transition(AbuseStatus.RESOLVED, at=NOW)
        assert moved.status is AbuseStatus.RESOLVED
        assert moved.resolved_at == NOW

    def test_open_cannot_skip_to_resolved(self) -> None:
        with pytest.raises(InvalidAbuseTransition):
            _case().transition(AbuseStatus.RESOLVED, at=NOW)

    def test_terminal_states_reject_all_moves(self) -> None:
        for terminal in (AbuseStatus.RESOLVED, AbuseStatus.DISMISSED):
            case = _case(
                resource=_ref(),
                user_id=uuid4(),
                reason="r",
                reporter="x",
                status=terminal,
            )
            for target in AbuseStatus:
                assert case.can_transition_to(target) is False
                with pytest.raises(InvalidAbuseTransition):
                    case.transition(target, at=NOW)

    def test_original_case_unchanged_after_transition(self) -> None:
        original = _case()
        original.transition(AbuseStatus.INVESTIGATING, at=NOW)
        assert original.status is AbuseStatus.OPEN
        assert original.updated_at is None

    def test_mapping_fields_survive_transition(self) -> None:
        user_id = uuid4()
        server_id = uuid4()
        case = _case(user_id=user_id, server_id=server_id)
        moved = case.transition(AbuseStatus.INVESTIGATING, at=NOW)
        assert moved.user_id == user_id
        assert moved.server_id == server_id
        assert moved.resource == case.resource
