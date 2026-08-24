"""Tests for the web panel shells (M14-004 customer, M14-005 admin).

M14-004 acceptance: **login/session and server/wallet views.**
- Login validates an API token through the SAME hashed-token
  authenticator as bearer auth and sets an HttpOnly session cookie;
  bad tokens re-render the form; logout clears the cookie; a revoked
  token kills the session on its next request.
- The panel view lists the user's servers (ownership-scoped) and their
  wallet balance.

M14-005 acceptance: **provider/users/operations dashboards.**
- The admin dashboard requires an ACTIVE ADMIN behind the cookie;
  anonymous AND non-admin sessions are redirected identically.
- Dashboards render users search, recent audit operations and provider
  capabilities.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cloud_platform.api.app import create_app
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope
from cloud_platform.modules.users.domain import Role, UserStatus

RAW_TOKEN = "cpt_testtoken123"


class FakeUser:
    def __init__(self, *, role: Role = Role.USER, status: UserStatus = UserStatus.ACTIVE):
        from uuid import uuid4

        self.id = uuid4()
        self.username = f"user-{self.id.hex[:6]}"
        self.email = f"{self.username}@example.com"
        self.role = role
        self.status = status

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    def has_permission(self, permission: object) -> bool:
        del permission  # admins hold every admin permission in this fake
        return self.role is Role.ADMIN


class FakeServerRow:
    def __init__(self) -> None:
        from datetime import UTC, datetime
        from uuid import uuid4

        self.server_id = uuid4()
        self.provider_key = "hetzner"
        self.state = "running"
        self.created_at = datetime.now(UTC)


class FakeServersPage:
    def __init__(self) -> None:
        self.items = [FakeServerRow()]
        self.total = 1
        self.offset = 0
        self.limit = 20


class FakeAuditEvent:
    from datetime import UTC, datetime

    occurred_at = datetime.now(UTC)
    actor_type = type("T", (), {"value": "system"})()
    action = "volume.reconciled"
    resource_type = "volume"
    reason = "drill"


@pytest.fixture()
def web():
    app = create_app()
    users = FakeUserRepo()
    servers = FakeServersPage()
    tokens: dict[str, TokenAuthentication] = {}
    audit_events: list[FakeAuditEvent] = [FakeAuditEvent()]

    async def authenticate(raw_token: str) -> TokenAuthentication:
        entry = tokens.get(raw_token)
        if entry is None:
            raise LookupError("invalid or revoked token")
        return entry

    app.state.authenticate_token = authenticate

    from cloud_platform.api.web import admin as admin_web
    from cloud_platform.api.web import customer as customer_web

    async def _authenticator() -> object:
        return authenticate

    async def _servers_view() -> object:
        class _Service:
            async def list_servers(self, _user_id: object) -> FakeServersPage:
                return servers

        return _Service()

    async def _wallet_view() -> object:
        async def _load(_user_id: object) -> FakeWallet:
            return FakeWallet()

        return _load

    async def _user_repo() -> FakeUserRepo:
        return users

    async def _user_search() -> object:
        async def _search(q: str = "", *, offset: int = 0, limit: int = 20) -> list[FakeUser]:
            del q, offset, limit
            return [users.admin]

        return _search

    async def _audit_feed() -> object:
        async def _load(limit: int) -> list[FakeAuditEvent]:
            return audit_events[:limit]

        return _load

    async def _providers_view() -> object:
        async def _load() -> list[tuple[str, str]]:
            return [("hetzner", "list_locations, list_plans")]

        return _load

    customer_web.router  # noqa: B018 - imported for override key identity
    app.dependency_overrides[customer_web._authenticator] = _authenticator
    app.dependency_overrides[customer_web._servers_view] = _servers_view
    app.dependency_overrides[customer_web._wallet_view] = _wallet_view
    app.dependency_overrides[admin_web._user_repo] = _user_repo
    app.dependency_overrides[admin_web._user_search] = _user_search
    app.dependency_overrides[admin_web._audit_feed] = _audit_feed
    app.dependency_overrides[admin_web._providers_view] = _providers_view

    client = TestClient(app)
    client.users = users  # type: ignore[attr-defined]
    client.tokens = tokens  # type: ignore[attr-defined]

    def register(raw: str, user: FakeUser) -> None:
        tokens[raw] = TokenAuthentication(
            user_id=user.id,
            scopes=frozenset(TokenScope),
            token_id=user.id,
        )

    client.register_token = register  # type: ignore[attr-defined]
    yield client

    app.dependency_overrides.clear()


class FakeWallet:
    balance = 123_45
    currency = "EUR"


class FakeUserRepo:
    """Just enough of SqlAlchemyUserRepository for the panels."""

    def __init__(self) -> None:
        self.admin = FakeUser(role=Role.ADMIN)
        self.plain = FakeUser()

    async def get(self, user_id: object) -> FakeUser | None:
        for u in (self.admin, self.plain):
            if u.id == user_id:
                return u
        return None

    async def search(self, _q: str, *, offset: int = 0, limit: int = 20) -> list[FakeUser]:
        del offset
        return [self.admin][:limit]


class TestCustomerPanelM14004:
    def test_anonymous_panel_redirects_to_login(self, web):  # type: ignore[no-untyped-def]
        response = web.get("/web/panel", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/web/login"

    def test_login_form_renders(self, web):  # type: ignore[no-untyped-def]
        response = web.get("/web/login")
        assert response.status_code == 200
        assert "API token" in response.text
        assert 'name="token"' in response.text

    def test_bad_token_renders_error_not_a_session(self, web):  # type: ignore[no-untyped-def]
        response = web.post("/web/login", data={"token": "cpt_wrong"})
        assert response.status_code == 200
        assert "not accepted" in response.text
        assert "platform_session" not in response.headers.get("set-cookie", "")

    def test_valid_token_starts_httponly_session(self, web):  # type: ignore[no-untyped-def]
        web.register_token(RAW_TOKEN, web.users.plain)
        response = web.post("/web/login", data={"token": RAW_TOKEN}, follow_redirects=False)
        assert response.status_code == 303
        assert "/web/panel" in response.headers["location"]
        cookie_header = response.headers["set-cookie"]
        assert "platform_session=" in cookie_header
        assert "HttpOnly" in cookie_header.replace("httponly", "HttpOnly")

    def test_panel_lists_servers_and_wallet(self, web):  # type: ignore[no-untyped-def]
        web.register_token(RAW_TOKEN, web.users.plain)
        web.cookies.set("platform_session", RAW_TOKEN)
        response = web.get("/web/panel")
        assert response.status_code == 200
        assert "Your servers" in response.text
        assert "hetzner" in response.text
        assert "running" in response.text
        assert "Wallet" in response.text
        assert "12345" in response.text  # minor units verbatim
        assert "123.45 EUR" in response.text  # major-unit hint

    def test_revoked_token_kills_the_session_next_request(self, web):  # type: ignore[no-untyped-def]
        web.register_token("cpt_dying", web.users.plain)
        web.cookies.set("platform_session", "cpt_dying")
        assert web.get("/web/panel").status_code == 200
        del web.tokens["cpt_dying"]  # revoke server-side
        response = web.get("/web/panel", follow_redirects=False)
        assert response.status_code == 303  # bounced to login

    def test_logout_clears_cookie(self, web):  # type: ignore[no-untyped-def]
        response = web.get("/web/logout", follow_redirects=False)
        assert response.status_code == 303


class TestAdminPanelM14005:
    def test_anonymous_redirected_like_customer(self, web):  # type: ignore[no-untyped-def]
        response = web.get("/web/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/web/login"

    def test_non_admin_redirected_identically(self, web):  # type: ignore[no-untyped-def]
        web.register_token(RAW_TOKEN, web.users.plain)
        web.cookies.set("platform_session", RAW_TOKEN)
        response = web.get("/web/admin", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/web/login"

    def test_admin_sees_all_dashboards(self, web):  # type: ignore[no-untyped-def]
        web.register_token(RAW_TOKEN, web.users.admin)
        web.cookies.set("platform_session", RAW_TOKEN)
        response = web.get("/web/admin")
        assert response.status_code == 200
        assert "Users" in response.text
        assert web.users.admin.username in response.text
        assert "Recent operations" in response.text
        assert "volume.reconciled" in response.text
        assert "Providers" in response.text
        assert "hetzner" in response.text

    def test_search_filter_reaches_the_users_dashboard(self, web):  # type: ignore[no-untyped-def]
        web.register_token(RAW_TOKEN, web.users.admin)
        web.cookies.set("platform_session", RAW_TOKEN)
        response = web.get("/web/admin?q=admin")
        assert response.status_code == 200
        assert web.users.admin.username in response.text
