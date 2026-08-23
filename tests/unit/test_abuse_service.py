"""Tests for AbuseIntakeService: resource-to-user mapping + audit linkage (M10-006)."""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.abuse.domain import (
    AbuseCase,
    AbuseStatus,
    InvalidAbuseTransition,
    Ownership,
    ResourceNotManagedError,
    ResourceRef,
    ResourceType,
)
from cloud_platform.modules.abuse.service import AbuseIntakeService
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User

USER_ID = uuid4()
SERVER_ID = uuid4()
ADMIN_ID = uuid4()
REF = ResourceRef("hetzner", ResourceType.PROVIDER_SERVER, "srv-42")


def _admin(role: Role = Role.ADMIN) -> User:
    return User(id=ADMIN_ID, username="boss", email="boss@example.com", role=role)


def _service(resolver: object, cases: object, audit: object) -> AbuseIntakeService:
    return AbuseIntakeService(resolver, cases, audit)  # type: ignore[arg-type]


def _ownership() -> Ownership:
    return Ownership(user_id=USER_ID, server_id=SERVER_ID)


class TestIntake:
    async def test_external_report_maps_user_and_audits(self) -> None:
        """No actor: treated as an external provider report (SYSTEM actor)."""
        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=_ownership())
        cases = AsyncMock()
        cases.create = AsyncMock(side_effect=lambda c: dataclasses.replace(c, id=uuid4()))
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        case = await service.intake(ref=REF, reason="port scan observed", reporter="hetzner-abuse")

        assert case.user_id == USER_ID
        assert case.server_id == SERVER_ID
        assert case.id is not None

        event = audit.append.call_args[0][0]
        assert event.action == "abuse.intake"
        assert event.actor_type.value == "system"
        assert event.actor_id is None
        assert event.resource_type == "user"
        assert event.resource_id == str(USER_ID)
        assert event.reason == "port scan observed"
        assert event.metadata["provider_key"] == "hetzner"
        assert event.metadata["resource"] == "provider_server:srv-42"

    async def test_admin_report_audits_admin_actor(self) -> None:
        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=_ownership())
        cases = AsyncMock()
        cases.create = AsyncMock(side_effect=lambda c: dataclasses.replace(c, id=uuid4()))
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        await service.intake(
            ref=REF, reason="manual report", reporter="ops@platform", actor=_admin()
        )

        event = audit.append.call_args[0][0]
        assert event.actor_type.value == "admin"
        assert event.actor_id == ADMIN_ID

    async def test_non_admin_cannot_report(self) -> None:
        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=_ownership())
        cases = AsyncMock()
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        with pytest.raises(PermissionDeniedError):
            await service.intake(ref=REF, reason="x", reporter="y", actor=_admin(Role.USER))
        resolver.resolve.assert_not_awaited()
        cases.create.assert_not_awaited()

    async def test_unmanaged_resource_raises_without_persistence(self) -> None:
        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=None)
        cases = AsyncMock()
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        with pytest.raises(ResourceNotManagedError):
            await service.intake(ref=REF, reason="x", reporter="hetzner-abuse")
        cases.create.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_empty_reason_rejected_by_domain(self) -> None:
        resolver = AsyncMock()
        resolver.resolve = AsyncMock(return_value=_ownership())
        cases = AsyncMock()
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        with pytest.raises(ValueError, match="reason"):
            await service.intake(ref=REF, reason="  ", reporter="hetzner-abuse")
        cases.create.assert_not_awaited()


class TestTransition:
    async def test_valid_transition_audits_and_persists(self) -> None:
        resolver = AsyncMock()
        cases = AsyncMock()
        case = AbuseCase(
            resource=REF,
            user_id=USER_ID,
            server_id=SERVER_ID,
            reason="port scan",
            reporter="hetzner-abuse",
            id=uuid4(),
        )
        cases.get = AsyncMock(return_value=case)
        cases.save = AsyncMock(side_effect=lambda c: c)
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        moved = await service.transition(
            case_id=case.id,  # type: ignore[arg-type]
            target=AbuseStatus.INVESTIGATING,
            reason="started packet capture",
            actor=_admin(),
        )

        assert moved.status is AbuseStatus.INVESTIGATING
        cases.save.assert_awaited_once()
        event = audit.append.call_args[0][0]
        assert event.action == "abuse.transition"
        assert event.metadata == {
            "from": "open",
            "to": "investigating",
        }

    async def test_invalid_transition_raises_without_persistence(self) -> None:
        resolver = AsyncMock()
        cases = AsyncMock()
        case = AbuseCase(
            resource=REF,
            user_id=USER_ID,
            reason="r",
            reporter="x",
            id=uuid4(),
        )
        cases.get = AsyncMock(return_value=case)
        cases.save = AsyncMock()
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        with pytest.raises(InvalidAbuseTransition):
            await service.transition(
                case_id=case.id,  # type: ignore[arg-type]
                target=AbuseStatus.RESOLVED,  # open cannot skip to resolved
                reason="skip",
            )
        cases.save.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_missing_case_raises_lookup_error(self) -> None:
        resolver = AsyncMock()
        cases = AsyncMock()
        cases.get = AsyncMock(return_value=None)
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        with pytest.raises(LookupError, match="not found"):
            await service.transition(case_id=uuid4(), target=AbuseStatus.INVESTIGATING, reason="x")

    async def test_non_admin_cannot_transition(self) -> None:
        resolver = AsyncMock()
        cases = AsyncMock()
        audit = AsyncMock()

        service = _service(resolver, cases, audit)
        with pytest.raises(PermissionDeniedError):
            await service.transition(
                case_id=uuid4(),
                target=AbuseStatus.INVESTIGATING,
                reason="x",
                actor=_admin(Role.USER),
            )
        cases.get.assert_not_awaited()
