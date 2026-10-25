"""Settings and recharge read models must not mutate financial/customer records."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.api.v1 import console
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope


async def test_settings_http_auth_scope_and_current_bearer_revocation(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from cloud_platform.api.v1.errors import install_error_handlers

    auth = TokenAuthentication(
        user_id=uuid4(), token_id=uuid4(), scopes=frozenset({TokenScope.SERVERS_READ})
    )
    service = SimpleNamespace(revoke=AsyncMock())
    monkeypatch.setattr(console, "get_settings", lambda: SimpleNamespace(app_env="staging"))
    monkeypatch.setattr(
        console,
        "get_container",
        AsyncMock(return_value=SimpleNamespace(token_service=lambda: service)),
    )
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(console.router)
    app.state.authenticate_token = AsyncMock(return_value=auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        client.headers.update({"X-Platform-Contract": "1", "X-Platform-Environment": "staging"})
        assert (await client.get("/v1/console/charge-requests")).status_code == 401
        assert (await client.post("/v1/console/link/revoke")).status_code == 401
        client.headers["Authorization"] = "Bearer cpt_fixture"
        assert (await client.get("/v1/console/charge-requests")).status_code == 403
        assert (await client.post("/v1/console/link/revoke")).status_code == 428
        service.revoke.assert_not_awaited()
        client.headers["Idempotency-Key"] = "fixture-request-key"
        response = await client.post("/v1/console/link/revoke", json={"token_id": str(uuid4())})
        assert response.status_code == 200
        service.revoke.assert_awaited_once_with(actor_user_id=auth.user_id, token_id=auth.token_id)


async def test_revoke_only_uses_current_authenticated_token(monkeypatch):
    auth = TokenAuthentication(
        user_id=uuid4(), token_id=uuid4(), scopes=frozenset({TokenScope.SERVERS_READ})
    )
    service = SimpleNamespace(revoke=AsyncMock())
    container = SimpleNamespace(token_service=lambda: service)
    monkeypatch.setattr(console, "get_container", AsyncMock(return_value=container))
    assert await console.revoke_website_link(auth, "fixture-request-key") == {
        "data": {"revoked": True}
    }
    service.revoke.assert_awaited_once_with(actor_user_id=auth.user_id, token_id=auth.token_id)


async def test_payment_history_is_owner_scoped_bounded_and_keeps_snapshot_currency(monkeypatch):
    from sqlalchemy.dialects import postgresql

    auth = TokenAuthentication(
        user_id=uuid4(), token_id=uuid4(), scopes=frozenset({TokenScope.WALLET_READ})
    )
    row = SimpleNamespace(
        id=uuid4(),
        amount_minor=500,
        currency="USD",
        credit_amount_minor=300000,
        credit_currency="IRT",
        status="succeeded",
        credited_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [row])
    )
    context = AsyncMock()
    context.__aenter__.return_value = session
    container = SimpleNamespace(session_factory=lambda: context)
    monkeypatch.setattr(console, "get_container", AsyncMock(return_value=container))
    monkeypatch.setattr(
        console, "active_user", AsyncMock(return_value=SimpleNamespace(id=auth.user_id))
    )
    result = await console.charge_requests(auth)
    payment = result["data"][0]
    assert payment["amount"] == 300000 and payment["currency"] == "IRT"
    assert payment["status"] == "approved"
    assert "invoiceNumber" not in payment
    sql = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "payment_sessions.user_id =" in str(sql)
    assert auth.user_id in sql.params.values() and 50 in sql.params.values()
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    "status,credited,expected",
    [
        ("succeeded", None, "processing"),
        ("manual_review", None, "processing"),
        ("failed", None, "rejected"),
        ("pending", None, "pending"),
    ],
)
async def test_payment_status_does_not_claim_uncredited_funds(
    monkeypatch, status, credited, expected
):
    auth = TokenAuthentication(
        user_id=uuid4(), token_id=uuid4(), scopes=frozenset({TokenScope.WALLET_READ})
    )
    row = SimpleNamespace(
        id=uuid4(),
        amount_minor=500,
        currency="USD",
        credit_amount_minor=None,
        credit_currency=None,
        status=status,
        credited_at=credited,
        created_at=datetime.now(UTC),
    )
    session = AsyncMock()
    session.execute.return_value = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [row])
    )
    context = AsyncMock()
    context.__aenter__.return_value = session
    monkeypatch.setattr(
        console,
        "get_container",
        AsyncMock(return_value=SimpleNamespace(session_factory=lambda: context)),
    )
    monkeypatch.setattr(
        console, "active_user", AsyncMock(return_value=SimpleNamespace(id=auth.user_id))
    )
    result = (await console.charge_requests(auth))["data"][0]
    assert result["status"] == expected
    assert result["amount"] == 500 and result["currency"] == "USD"
