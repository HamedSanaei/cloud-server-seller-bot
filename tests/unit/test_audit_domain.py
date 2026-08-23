"""Tests for the audit domain: immutable event records."""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from cloud_platform.modules.audit.domain import ActorType, AuditEvent


class TestAuditEventConstruction:
    def test_valid_event(self) -> None:
        event = AuditEvent(
            actor_type=ActorType.ADMIN,
            action="wallet.adjust",
            resource_type="wallet",
            actor_id=uuid4(),
            resource_id=str(uuid4()),
            reason="compensation for outage",
            metadata={"amount": "500"},
        )
        assert event.action == "wallet.adjust"
        assert event.actor_type is ActorType.ADMIN
        assert event.id is None  # assigned on persistence
        assert event.metadata == {"amount": "500"}

    def test_empty_action_rejected(self) -> None:
        with pytest.raises(ValueError, match="action"):
            AuditEvent(actor_type=ActorType.SYSTEM, action="  ", resource_type="wallet")

    def test_empty_resource_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="resource_type"):
            AuditEvent(actor_type=ActorType.SYSTEM, action="wallet.adjust", resource_type="")

    def test_defaults(self) -> None:
        event = AuditEvent(
            actor_type=ActorType.SYSTEM,
            action="job.run",
            resource_type="catalog",
        )
        assert event.reason == ""
        assert event.resource_id == ""
        assert event.metadata == {}
        assert event.occurred_at is None

    def test_metadata_dicts_are_independent(self) -> None:
        a = AuditEvent(actor_type=ActorType.USER, action="a", resource_type="r")
        b = AuditEvent(actor_type=ActorType.USER, action="a", resource_type="r")
        a.metadata["k"] = "v"
        assert b.metadata == {}

    def test_event_is_frozen(self) -> None:
        event = AuditEvent(actor_type=ActorType.USER, action="a", resource_type="r")
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.action = "mutated"  # type: ignore[misc]


class TestActorType:
    def test_values_match_schema_strings(self) -> None:
        assert ActorType.USER.value == "user"
        assert ActorType.ADMIN.value == "admin"
        assert ActorType.SYSTEM.value == "system"
