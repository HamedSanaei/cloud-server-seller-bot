"""Tests for the admin API (M14-003).

Acceptance: capabilities AND active admin RBAC gate every request.

GETs require admin:read; mutations require admin:write and delegate a
reason-bound, actor-scoped idempotent status command to the repository.
The PostgreSQL suite proves atomic audit persistence and replay semantics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud_platform.api.v1 import router as v1_router
from cloud_platform.api.v1.admin import (
    _audit_repo,
    _configured_superadmin_ids,
    _user_repo,
)
from cloud_platform.api.v1.dependencies import get_token_authentication
from cloud_platform.modules.audit.domain import ActorType, AuditEvent
from cloud_platform.modules.audit.service import AuditTrail
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
    UserNotFound,
    UserStatus,
)
from cloud_platform.modules.users.repository import UserStatusIdempotencyConflict

ADMIN_ID = uuid4()
USER_ID = uuid4()
TARGET_ID = uuid4()


class FakeUserRepo:
    def __init__(self) -> None:
        self.users: dict[UUID, User] = {}
        self.change_status_idempotently = AsyncMock(return_value=UserStatus.FROZEN)

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
        telegram_user_id=77110022,
    )


def make_plain() -> User:
    return User(id=USER_ID, username="joe", email="joe@example.com", role=Role.USER)


def build_app(
    scopes: frozenset[TokenScope] = frozenset({TokenScope.ADMIN_READ, TokenScope.ADMIN_WRITE}),
) -> tuple[TestClient, FakeUserRepo, FakeAuditRepo]:
    app = FastAPI()
    app.include_router(v1_router)
    from cloud_platform.api.v1.admin import router as admin_router

    app.include_router(admin_router)
    from cloud_platform.api.v1.errors import install_error_handlers

    install_error_handlers(app)

    app.dependency_overrides[_configured_superadmin_ids] = lambda: (77110022,)
    users = FakeUserRepo()
    audits = FakeAuditRepo()
    auth = token_auth(ADMIN_ID, scopes)
    users.users[ADMIN_ID] = make_admin()  # the acting admin exists
    app.dependency_overrides[get_token_authentication] = lambda: auth
    app.dependency_overrides[_user_repo] = lambda: users
    app.dependency_overrides[_audit_repo] = lambda: audits
    return TestClient(app), users, audits  # type: ignore[return-value]


def token_auth(
    user_id: UUID,
    scopes: frozenset[TokenScope] = frozenset({TokenScope.ADMIN_READ, TokenScope.ADMIN_WRITE}),
) -> TokenAuthentication:
    return TokenAuthentication(user_id=user_id, scopes=scopes, token_id=uuid4())


class TestRbacGate:
    def test_configuration_removal_revokes_legacy_admin_immediately(self) -> None:
        client, users, _ = build_app()
        client.app.dependency_overrides[_configured_superadmin_ids] = lambda: ()
        assert client.get("/v1/admin/users").status_code == 403
        assert (
            client.post(
                f"/v1/admin/users/{TARGET_ID}/status",
                headers={"Idempotency-Key": "removed"},
                json={"status": "active", "reason": "reviewed"},
            ).status_code
            == 403
        )
        users.change_status_idempotently.assert_not_awaited()

    def test_telegram_identity_headers_and_payload_cannot_elevate_token_user(self) -> None:
        client, users, _ = build_app()
        users.users[USER_ID] = make_plain()
        client.app.dependency_overrides[get_token_authentication] = lambda: token_auth(USER_ID)
        headers = {
            "X-Telegram-User-Id": "77110022",
            "X-User-Id": str(ADMIN_ID),
            "Idempotency-Key": "forged",
        }
        assert client.get("/v1/admin/users", headers=headers).status_code == 403
        assert (
            client.post(
                f"/v1/admin/users/{TARGET_ID}/status",
                headers=headers,
                json={
                    "status": "active",
                    "reason": "reviewed",
                    "role": "admin",
                    "telegram_user_id": 77110022,
                    "actor_id": str(ADMIN_ID),
                },
            ).status_code
            == 403
        )
        users.change_status_idempotently.assert_not_awaited()

    def test_non_admin_is_forbidden_envelope(self) -> None:
        client, users, _audits = build_app()
        users.users[TARGET_ID] = User(
            id=TARGET_ID, username="t", email="t@example.com", role=Role.USER
        )
        # override identity to a NON-admin user that exists
        plain = make_plain()
        users.users[USER_ID] = plain
        client.app.dependency_overrides[get_token_authentication] = lambda: token_auth(USER_ID)  # type: ignore[attr-defined]
        resp = client.get(f"/v1/admin/users/{TARGET_ID}")
        assert resp.status_code == 403
        error = resp.json()["error"]
        assert error["code"] == "forbidden"

    def test_unknown_actor_is_forbidden_too(self) -> None:
        client, _users, _audits = build_app()
        client.app.dependency_overrides[get_token_authentication] = lambda: token_auth(uuid4())  # type: ignore[attr-defined]
        resp = client.get("/v1/admin/users")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

    @pytest.mark.parametrize("status", [UserStatus.FROZEN, UserStatus.BANNED])
    def test_inactive_admin_cannot_read_or_mutate(self, status: UserStatus) -> None:
        client, users, _audits = build_app()
        users.users[ADMIN_ID].status = status
        assert client.get("/v1/admin/users").status_code == 403
        response = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "inactive"},
            json={"status": "active", "reason": "reviewed"},
        )
        assert response.status_code == 403
        users.change_status_idempotently.assert_not_awaited()


class TestTokenCapabilities:
    @pytest.mark.parametrize(
        "scopes",
        [
            frozenset(),
            frozenset({TokenScope.CATALOG_READ}),
            frozenset({TokenScope.ADMIN_WRITE}),
        ],
    )
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/admin/users",
            f"/v1/admin/users/{TARGET_ID}",
            f"/v1/admin/audit?actor_id={ADMIN_ID}",
        ],
    )
    def test_gets_reject_tokens_without_admin_read(
        self, scopes: frozenset[TokenScope], path: str
    ) -> None:
        client, _users, _audits = build_app(scopes)
        response = client.get(path)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"

    @pytest.mark.parametrize(
        "scopes",
        [
            frozenset(),
            frozenset({TokenScope.SERVERS_WRITE}),
            frozenset({TokenScope.ADMIN_READ}),
        ],
    )
    def test_mutation_rejects_tokens_without_admin_write(
        self, scopes: frozenset[TokenScope]
    ) -> None:
        client, users, audits = build_app(scopes)
        response = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "restricted"},
            json={"status": "active", "reason": "reviewed"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"
        users.change_status_idempotently.assert_not_awaited()
        assert audits.events == []

    def test_read_only_admin_can_read(self) -> None:
        client, _users, _audits = build_app(frozenset({TokenScope.ADMIN_READ}))
        assert client.get("/v1/admin/users").status_code == 200

    def test_write_only_admin_can_mutate(self) -> None:
        client, users, _audits = build_app(frozenset({TokenScope.ADMIN_WRITE}))
        response = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "write-only"},
            json={"status": "frozen", "reason": "reviewed"},
        )
        assert response.status_code == 200
        users.change_status_idempotently.assert_awaited_once()


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
    def test_revocation_during_command_returns_forbidden(self) -> None:
        client, users, _ = build_app()
        users.change_status_idempotently.side_effect = PermissionDeniedError("grant revoked")
        response = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "revoked"},
            json={"status": "active", "reason": "reviewed"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"

    def test_status_change_requires_reason_and_delegates_atomic_audit(self) -> None:
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
        users.change_status_idempotently.assert_not_awaited()

        ok = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "a2"},
            json={"status": "frozen", "reason": "fraud report"},
        )
        assert ok.status_code == 200
        assert ok.json()["user"]["status"] == "frozen"
        users.change_status_idempotently.assert_awaited_once_with(
            TARGET_ID,
            UserStatus.FROZEN,
            actor_id=ADMIN_ID,
            idempotency_key="a2",
            reason="fraud report",
            admin_configured_ids=(77110022,),
        )
        # The endpoint must not append an independently committed second audit.
        assert audits.events == []

    def test_conflicting_key_returns_conflict_envelope(self) -> None:
        client, users, _audits = build_app()
        users.change_status_idempotently.side_effect = UserStatusIdempotencyConflict(
            "status-change idempotency key carries different facts"
        )
        response = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "reused"},
            json={"status": "banned", "reason": "reviewed"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    def test_missing_target_returns_not_found_envelope(self) -> None:
        client, users, _audits = build_app()
        users.change_status_idempotently.side_effect = UserNotFound("missing target")
        response = client.post(
            f"/v1/admin/users/{TARGET_ID}/status",
            headers={"Idempotency-Key": "missing"},
            json={"status": "active", "reason": "reviewed"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

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
