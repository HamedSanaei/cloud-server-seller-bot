"""Reserved-resource delegation; all provider/financial collaborators are fakes."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.dialects import postgresql

from cloud_platform.api.v1 import console
from cloud_platform.api.v1.errors import install_error_handlers
from cloud_platform.db.base import AuditEvent, Server, ServerAccessGrant
from cloud_platform.db.base import User as UserRow
from cloud_platform.modules.funding.domain import FundingSource
from cloud_platform.modules.servers import delegation_repository as storage
from cloud_platform.modules.servers.delegation import binding, permissions, validate_operations
from cloud_platform.modules.servers.models import (
    CustomerServerPage,
    CustomerServerState,
    CustomerServerView,
    ServerOperation,
)
from cloud_platform.modules.servers.service import ServerManagementError
from cloud_platform.modules.servers.test_identity import TEST_IPV4, TEST_PROVIDER_ID, is_test_server
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope
from cloud_platform.modules.users.domain import PermissionDeniedError, Role, User
from tests.unit.test_servers_management_service import make_server, make_service


def reserved():
    server = make_server(provider_server_id=TEST_PROVIDER_ID)
    server.ipv4 = TEST_IPV4
    server.funding_source = FundingSource.OPERATOR
    server.funding_operator_id = server.user_id
    server.credential_account_id = "default"
    return server


def grant_for(server, user_id):
    return SimpleNamespace(
        server_id=server.id,
        delegate_user_id=user_id,
        owner_user_id=server.user_id,
        binding=binding(server),
        operations=["view", "refresh"],
        revoked_at=None,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_server_id", "other-vps"),
        ("ipv4", "192.0.2.45"),
        ("funding_source", FundingSource.CUSTOMER_WALLET),
        ("funding_operator_id", uuid4()),
        ("credential_account_id", "other-account"),
        ("user_id", uuid4()),
    ],
)
def test_identity_and_funding_drift_never_inherit_grants(field, value):
    server, user_id = reserved(), uuid4()
    grant = grant_for(server, user_id)
    assert permissions(grant, server, user_id) == {"view", "refresh"}
    setattr(server, field, value)
    assert not permissions(grant, server, user_id)


@pytest.mark.parametrize(
    "operations",
    [
        ["view", "delete"],
        ["view", "start"],
        ["view", "renew_now"],
        ["view", "password_reset"],
        ["view", "console"],
        [],
        ["refresh"],
        {"view": True},
    ],
)
def test_grants_cannot_escalate_into_mutation_or_billing(operations):
    with pytest.raises(ValueError):
        validate_operations(operations)


@pytest.fixture
def delegated_http(monkeypatch):
    user = User(id=uuid4(), username="delegated", email="linked@example.test", telegram_user_id=99)
    server = reserved()
    grant = grant_for(server, user.id)
    access = SimpleNamespace(
        permissions=AsyncMock(side_effect=lambda uid, item: permissions(grant, item, uid)),
        server_ids=AsyncMock(
            side_effect=lambda uid: [server.id] if permissions(grant, server, uid) else []
        ),
        change=AsyncMock(),
    )
    view = CustomerServerView(server_id=server.id, state=CustomerServerState.RUNNING)
    management = SimpleNamespace(
        list_servers=AsyncMock(return_value=CustomerServerPage((), 1, 10, 0)),
        get_server=AsyncMock(return_value=view),
        available_operations=AsyncMock(return_value=frozenset({ServerOperation.VIEW})),
    )
    container = SimpleNamespace(
        user_repository=lambda: SimpleNamespace(get=AsyncMock(return_value=user)),
        server_repository=lambda: SimpleNamespace(get=AsyncMock(return_value=server)),
        server_access_repository=lambda: access,
        server_management_service=lambda **_: management,
    )
    monkeypatch.setattr(console, "get_container", AsyncMock(return_value=container))
    monkeypatch.setattr(
        console,
        "get_settings",
        lambda: SimpleNamespace(app_env="test", telegram_superadmin_user_ids=[42]),
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(console.router)
    app.state.authenticate_token = AsyncMock(
        return_value=TokenAuthentication(
            user_id=user.id, token_id=uuid4(), scopes=frozenset(TokenScope)
        )
    )
    return app, user, server, grant, access, management


async def test_console_list_detail_revocation_and_stranger(delegated_http):
    app, _user, server, grant, access, management = delegated_http
    before = deepcopy(server)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={
            "X-Platform-Contract": "1",
            "X-Platform-Environment": "test",
            "Authorization": "Bearer cpt_fixture",
        },
    ) as client:
        listed = await client.get("/v1/console/servers")
        assert listed.status_code == 200
        assert [row["server_id"] for row in listed.json()["data"]] == [str(server.id)]
        detail = await client.get(f"/v1/console/servers/{server.id}")
        assert detail.status_code == 200 and detail.json()["data"]["credentialsAllowed"] is False
        assert detail.json()["data"]["operations"] == ["view"]
        assert management.get_server.await_args.args == (server.user_id, server.id)
        grant.revoked_at = datetime.now(UTC)
        assert (await client.get("/v1/console/servers")).json()["data"] == []
        assert (await client.get(f"/v1/console/servers/{server.id}")).status_code == 404
        assert (await client.get(f"/v1/console/servers/{server.id}/operations")).status_code == 404
        grant.revoked_at = None
        grant.delegate_user_id = uuid4()
        assert (await client.get(f"/v1/console/servers/{server.id}")).status_code == 404
    assert server == before and is_test_server(server)
    access.change.assert_not_awaited()


@pytest.mark.parametrize(
    "suffix",
    [
        "actions/power-on",
        "actions/power-off",
        "actions/reboot",
        "actions/delete",
        "actions/reinstall",
        "actions/reset-password",
        "actions/ssh-port",
        "confirmations/delete",
        "confirmations/reinstall",
        "credentials/claim",
        "credentials/ack",
    ],
)
async def test_delegate_rejected_before_intent_confirmation_or_provider_work(
    delegated_http, suffix
):
    app, _, server, _, _, management = delegated_http
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={
            "X-Platform-Contract": "1",
            "X-Platform-Environment": "test",
            "Authorization": "Bearer cpt_fixture",
            "Idempotency-Key": "fixture-key",
        },
    ) as client:
        response = await client.post(
            f"/v1/console/servers/{server.id}/{suffix}",
            json={"claimId": str(uuid4())} if suffix.endswith("ack") else {},
        )
        assert response.status_code == 403
    management.get_server.assert_not_awaited()


async def test_grant_route_requires_current_superadmin_not_just_admin_scope(delegated_http):
    app, user, server, _, access, _ = delegated_http
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={
            "X-Platform-Contract": "1",
            "X-Platform-Environment": "test",
            "Authorization": "Bearer cpt_fixture",
            "Idempotency-Key": "fixture-key",
        },
    ) as client:
        path = f"/v1/console/admin/servers/{server.id}/delegations/{uuid4()}"
        assert (
            await client.post(path, json={"reason": "test access", "operations": ["view"]})
        ).status_code == 403
        user.role, user.telegram_user_id = Role.ADMIN, 42
        access.change.return_value = {"active": True}
        assert (
            await client.post(path, json={"reason": "test access", "operations": ["view"]})
        ).status_code == 200
        assert (
            await client.post(
                path, json={"reason": "test access", "operations": ["view", "delete"]}
            )
        ).status_code == 400
        access.change.assert_awaited_once()


async def test_compatibility_probe_cannot_create_a_grant_without_authentication(delegated_http):
    app, _, server, _, access, _ = delegated_http
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={
            "X-Platform-Contract": "1",
            "X-Platform-Environment": "test",
            "Authorization": "Bearer ",
            "Idempotency-Key": "compatibility-probe-no-bearer",
        },
    ) as client:
        result = await client.post(
            f"/v1/console/admin/servers/{server.id}/delegations/{uuid4()}",
            json={"operations": ["view"], "reason": "Unauthenticated compatibility probe"},
        )
        assert result.status_code == 401
    access.change.assert_not_awaited()


async def test_superadmin_visibility_does_not_depend_on_delegate_grant(delegated_http):
    _, user, server, grant, access, _ = delegated_http
    user.role, user.telegram_user_id = Role.ADMIN, 42
    grant.revoked_at = datetime.now(UTC)
    container = await console.get_container()
    assert await console.target_owner(container, user, server.id) == server.user_id
    access.permissions.assert_not_awaited()


async def test_view_only_service_does_not_implicitly_refresh_or_keep_revoked_access():
    server = reserved()
    service, provider, *_ = make_service(server=server)
    service._authorize_test_server = AsyncMock(return_value=True)
    service._test_server_operations = AsyncMock(return_value=frozenset({"view"}))
    view = await service.get_server(server.user_id, server.id)
    assert view.server_id == server.id and provider.calls == []
    assert await service.available_operations(server.user_id, server.id) <= {ServerOperation.VIEW}
    with pytest.raises(ServerManagementError):
        await service.refresh_server(server.user_id, server.id)
    service._authorize_test_server.return_value = False
    with pytest.raises(ServerManagementError):
        await service.get_server(server.user_id, server.id)
    assert provider.calls == []


async def test_application_service_blocks_every_write_even_with_owner_shim():
    server = reserved()
    service, provider, power, audit, sink = make_service(server=server)
    service._authorize_test_server = AsyncMock(return_value=True)
    service._test_server_operations = AsyncMock(return_value=frozenset({"view", "refresh"}))
    calls = [
        lambda: service.start_server(server.user_id, server.id),
        lambda: service.reboot_server(server.user_id, server.id),
        lambda: service.stop_server(server.user_id, server.id),
        lambda: service.reset_password(server.user_id, server.id, confirmation_token=None),
        lambda: service.delete_server(server.user_id, server.id, confirmation_token=None),
        lambda: service.reinstall(
            server.user_id, server.id, image_ref="fixture", confirmation_token=None
        ),
        lambda: service.claim_ssh_password(server.user_id, server.id),
        lambda: service.finish_ssh_password(server.user_id, server.id, uuid4(), delivered=True),
        lambda: service.set_auto_renew(server.user_id, server.id, enabled=True),
        lambda: service.renew_now(server.user_id, server.id, confirmation_token=None),
        lambda: service.issue_confirmation(server.user_id, server.id, ServerOperation.REINSTALL),
    ]
    before = deepcopy(server)
    for work in calls:
        with pytest.raises(ServerManagementError):
            await work()
    assert server == before and provider.calls == [] and power.calls == []
    assert audit.events == [] and sink.events == []


class Session:
    """Statement-aware fake; real PostgreSQL lock behavior has a separate opt-in test."""

    def __init__(self, server, user):
        self.server, self.user, self.grant, self.events = server, user, None, []
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    def begin(self):
        return self

    async def execute(self, statement):
        self.queries.append(statement)
        model = statement.column_descriptions[0]["entity"]
        result = None
        if model is Server:
            result = (self.server, "leaseweb")
        elif model is UserRow:
            result = self.user
        elif model is ServerAccessGrant:
            result = self.grant
        elif model is AuditEvent:
            key = statement.compile().params["param_1"]
            result = next((e for e in self.events if e.event_metadata["command"] == key), None)
        return SimpleNamespace(one_or_none=lambda: result, scalar_one_or_none=lambda: result)

    def add(self, value):
        if isinstance(value, ServerAccessGrant):
            self.grant = value
        elif isinstance(value, AuditEvent):
            self.events.append(value)
        else:
            pytest.fail("delegation attempted a financial or infrastructure write")

    async def flush(self):
        pass


async def test_read_queries_compile_for_postgres_and_require_active_recipient():
    class Reader(Session):
        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            assert "JOIN users" in sql and "users.status" in sql
            self.queries.append(sql)
            return SimpleNamespace(all=lambda: [], scalar_one_or_none=lambda: None)

    server = reserved()
    session = Reader(server, None)
    repository = storage.ServerAccessRepository(lambda: session)
    assert await repository.server_ids(uuid4()) == []
    assert not await repository.permissions(uuid4(), server)
    assert len(session.queries) == 2


async def test_transactional_idempotency_revocation_audit_and_no_financial_writes(monkeypatch):
    server, delegate, actor = reserved(), uuid4(), uuid4()
    session = Session(server, SimpleNamespace(id=delegate, status="active", telegram_user_id=99))
    authorize = AsyncMock()
    monkeypatch.setattr(storage, "authorize_superadmin_transaction", authorize)
    monkeypatch.setattr(storage, "_to_domain", lambda row, _: row)
    repository = storage.ServerAccessRepository(lambda: session)
    args = dict(
        actor_id=actor,
        server_id=server.id,
        user_id=delegate,
        operations=["view"],
        reason="Explicit fixture access",
        key="fixture-grant",
        configured_ids=[42],
    )
    before = deepcopy(server)
    result = await repository.change(**args)
    identifier = session.grant.id
    assert result["active"] and not result["replayed"]
    assert (await repository.change(**args))["replayed"]
    assert len(session.events) == 1 and session.grant.id == identifier
    with pytest.raises(ValueError, match="facts differ"):
        await repository.change(**{**args, "operations": ["view", "refresh"]})
    revoked = await repository.change(
        **{**args, "key": "fixture-revoke", "operations": [], "revoke": True}
    )
    assert not revoked["active"] and len(session.events) == 2
    # Replaying the original grant after revoke must NEVER resurrect it.
    assert not (await repository.change(**args))["active"]
    assert session.grant.id == identifier and session.grant.revoked_at is not None
    assert session.events[-1].event_metadata["before"] == ["view"]
    assert session.events[-1].event_metadata["after"] == []
    assert server == before
    authorize.side_effect = PermissionDeniedError("revoked administrator")
    count = len(session.queries)
    with pytest.raises(PermissionDeniedError):
        await repository.change(**{**args, "key": "fixture-new-grant"})
    assert len(session.queries) == count and len(session.events) == 2


async def test_administrator_can_revoke_a_drifted_resource_grant(monkeypatch):
    server, delegate = reserved(), uuid4()
    session = Session(server, SimpleNamespace(id=delegate, status="frozen", telegram_user_id=99))
    session.grant = grant_for(server, delegate)
    server.ipv4 = "192.0.2.45"
    monkeypatch.setattr(storage, "authorize_superadmin_transaction", AsyncMock())
    monkeypatch.setattr(storage, "_to_domain", lambda row, _: row)
    result = await storage.ServerAccessRepository(lambda: session).change(
        actor_id=uuid4(),
        server_id=server.id,
        user_id=delegate,
        operations=[],
        reason="Retire drifted grant",
        key="fixture-revoke-drift",
        configured_ids=[42],
        revoke=True,
    )
    assert not result["active"] and session.grant.revoked_at is not None
