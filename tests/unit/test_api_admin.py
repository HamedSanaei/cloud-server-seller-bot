"""Tests for the admin API (M14-003).

Acceptance: search/actions are RBAC/audited.

- RBAC in the application layer: only role=admin users pass
  (PermissionChecker.require); non-admins get 403 forbidden envelope,
  identical for unknown ids - no existence leak;
- user search/detail under admin:manage_users; audit query under
  admin:manage_settings;
- status changes REQUIRE a reason and are audited as ADMIN-actor events;
- mutations require the idempotency key.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_platform.api.v1 import router as v1_router
from cloud_platform.api.v1.admin import (
    _audit_repo,
    _user_repo,
)
from cloud_platform.api.v1.dependencies import get_token_authentication
from cloud_platform.modules.audit.domain import ActorType, AuditEvent
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.users.domain import Role, User, UserStatus

ADMIN_ID = uuid4()
USER_ID = uuid4()
TARGET_ID = uuid4()


class FakeUserRepo:
    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.updated_status: list[tuple[UUID, UserStatus]] = []

    async def get(self, user_id: UUID) -> User | None:
        return self.users.get(user_id)

    async def search(self, query: str, *, offset: int = 0, limit: int = 20) -> list[User]:
        needle = query.lower()
        matches = [
            u
            for u in self.users.values()
            if needle in u.username.lower() or needle in u.email.lower()
        ]
        return matches[offset : offset + limit]

    async def update_status(self, user_id: UUID, status: UserStatus) -> User:
        target = self.users[user_id]
        changed = User(
            id=target.id,
            username=target.username,
            email=target.email,
            status=status,
            role=target.role,
        )
        self.users[user_id] = changed
        self.updated_status.append((user_id, status))
        return changed


class FakeAuditRepo:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> AuditEvent:
        self.events.append(event)
        return event

    async def get_by_resource(self, resource_type: str, resource_id: str) -> list[AuditEvent]:
        return [
            e
            for e in self.events
            if e.resource_type == resource_type and e.resource_id == resource_id
        ]

    async def get_by_actor(self, actor_id: UUID) -> list[AuditEvent]:
        return [e for e in self.events if e.actor_id == actor_id]


def make_admin() -> User:
    return User(
        id=ADMIN_ID,
        username="root",
        email="root@example.com",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )


def make_plain() -> User:
    return User(id=USER_ID, username="joe", email="joe@example.com", role=Role.USER)


def build_app() -> tuple[TestClient, FakeUserRepo, FakeAuditRepo]:
    app = FastAPI()
    app.include_router(v1_router)
    from cloud_platform.api.v1.admin import router as admin_router

    app.include_router(admin_router)
    from cloud_platform.api.v1.errors import install_error_handlers

    install_error_handlers(app)

    users = FakeUserRepo()
    audits = FakeAuditRepo()
    auth = TokenAuthStub(user_id=ADMIN_ID)
    users.users[ADMIN_ID] = make_admin()  # the acting admin exists
    app.dependency_overrides[get_token_authentication] = lambda: auth
    app.dependency_overrides[_user_repo] = lambda: users
    app.dependency_overrides[_audit_repo] = lambda: audits
    return TestClient(app), users, audits  # type: ignore[return-value]


class TokenAuthStub:
    """Stands in for the token-auth dependency."""

    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        self.scopes: frozenset[Any] = frozenset()
        self.token_id = uuid4()

    def has(self, *_scopes: Any) -> bool:
        return True


class TestRbacGate:
    def test_non_admin_is_forbidden_envelope(self) -> None:
        client, users, _audits = build_app()
        users.users[TARGET_ID] = User(
            id=TARGET_ID, username="t", email="t@example.com", role=Role.USER
        )
        # override identity to a NON-admin user that exists
        plain = make_plain()
        users.users[USER_ID] = plain
        client.app.dependency_overrides[get_token_authentication] = lambda: TokenAuthStub(USER_ID)  # type: ignore[attr-defined]
        resp = client.get(f"/v1/admin/users/{TARGET_ID}")
        assert resp.status_code == 403
        error = resp.json()["error"]
        assert error["code"] == "forbidden"

    def test_unknown_actor_is_forbidden_too(self) -> None:
        client, _users, _audits = build_app()
        client.app.dependency_overrides[get_token_authentication] = lambda: TokenAuthStub(uuid4())  # type: ignore[attr-defined]
        resp = client.get("/v1/admin/users")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"


class TestSearchAndDetail:
    def test_search_matches_username_and_email_substrings(self) -> None:
        client, users, _audits = build_app()
        users.users[TARGET_ID] = User(
            id=TARGET_ID, username="alice", email="alice@corp.example", role=Role.USER
        )
        by_name = client.get("/v1/admin/users?q=ali")
        assert by_name.status_code == 200
        assert [u["id"] for u in by_name.json()["users"]] == [str(TARGET_ID)]
        by_email = client.get("/v1/admin/users?q=corp.example")
        assert len(by_email.json()["users"]) == 1

    def test_detail_unknown_user_is_404_envelope(self) -> None:
        client, _users, _audits = build_app()
        resp = client.get(f"/v1/admin/users/{uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


class TestStatusActionAudited:
    def test_status_change_requires_reason_and_audits(self) -> None:
        client, users, audits = build_app()
        users.users[TARGET_ID] = User(
            id=TARGET_ID, username="bad", email="bad@example.com", role=Role.USER
        )
        no_reason = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "a1"},
            json={"status": "suspended"},
        )
        assert no_reason.status_code == 400
        assert no_reason.json()["error"]["code"] == "validation_error"
        assert users.updated_status == []  # nothing happened

        ok = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "a2"},
            json={"status": "frozen", "reason": "fraud report"},
        )
        assert ok.status_code == 200
        assert ok.json()["user"]["status"] == "frozen"
        event = audits.events[-1]
        assert event.action == "admin.user.status_changed"
        assert event.actor_type is ActorType.ADMIN
        assert event.actor_id == ADMIN_ID
        assert event.reason == "fraud report"
        assert event.metadata == {"status": "frozen"}

    def test_mutations_require_idempotency_key(self) -> None:
        client, _users, _audits = build_app()
        resp = client.post(
            f"/v1/admin/users/{TARGET_ID}/status", json={"status": "active", "reason": "r"}
        )
        assert resp.status_code == 428
        assert resp.json()["error"]["code"] == "idempotency_required"


class TestAuditQuery:
    def test_query_by_resource_pair(self) -> None:
        client, _users, audits = build_app()
        trail = AuditTrail(audits)
        import asyncio

        asyncio.get_event_loop_policy()
        loop = __import__("asyncio").new_event_loop()
        loop.run_until_complete(
            trail.record_mutation(
                actor_type=ActorType.USER,
                action="firewall.created",
                resource_type="firewall",
                resource_id="fw-9",
                metadata={},
            )
        )
        loop.close()
        resp = client.get("/v1/admin/audit?resource_type=firewall&resource_id=fw-9")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["action"] == "firewall.created"

    def test_missing_filter_is_validation_error(self) -> None:
        client, _users, _audits = build_app()
        resp = client.get("/v1/admin/audit")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "validation_error"
