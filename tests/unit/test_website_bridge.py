"""Website bridge boundaries; no provider, Telegram or production DB traffic."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from cloud_platform.api.v1 import console
from cloud_platform.api.v1.errors import ApiError, install_error_handlers
from cloud_platform.core.session_store import InMemoryBotSessionStore
from cloud_platform.modules.businesslog.domain import (
    BusinessEvent,
    BusinessEventType,
    BusinessLogDispatcher,
    BusinessLogPolicy,
)
from cloud_platform.modules.notifications.customer import CustomerEventSink
from cloud_platform.modules.notifications.outbox import notice_row
from cloud_platform.modules.operations.domain import Operation, OperationStatus, OperationType
from cloud_platform.modules.servers import guest
from cloud_platform.modules.servers.commands import ManagementReceiptConflict, execute_management
from cloud_platform.modules.servers.models import ServerActionOutcome, ServerOperation
from cloud_platform.modules.servers.test_identity import TEST_IPV4, TEST_PROVIDER_ID, is_test_server
from cloud_platform.modules.tokens.domain import TokenAuthentication, TokenScope
from cloud_platform.modules.tokens.website import exchange_pairing, issue_pairing
from cloud_platform.modules.users.domain import Role, User, UserStatus


def customer(**kwargs):
    return User(
        id=uuid4(), username="fixture", email="fixture@example.test", telegram_user_id=42, **kwargs
    )


def fixture_server(user_id=None):
    owner_id = user_id or uuid4()
    return SimpleNamespace(
        id=uuid4(),
        user_id=owner_id,
        provider_key="leaseweb",
        provider_server_id=TEST_PROVIDER_ID,
        ipv4=TEST_IPV4,
        funding_source="operator",
        credential_account_id="verified-account",
        funding_operator_id=owner_id,
    )


@pytest.fixture
def pairing():
    user = customer()
    store = InMemoryBotSessionStore()
    tokens = SimpleNamespace(create=AsyncMock(return_value=(None, "cpt_test-only")))
    users = SimpleNamespace(get=AsyncMock(return_value=user))
    container = SimpleNamespace(
        session_store=lambda: store, user_repository=lambda: users, token_service=lambda: tokens
    )
    return user, container, tokens


async def test_pairing_single_use_and_scoped(pairing):
    user, container, tokens = pairing
    code = await issue_pairing(container, user)
    assert len(code) == 47 and code.startswith("cpl_")
    linked, raw = await exchange_pairing(container, code, uuid4())
    assert linked.id == user.id and raw.startswith("cpt_")
    scopes = tokens.create.call_args.kwargs["scopes"]
    assert TokenScope.WALLET_READ in scopes and TokenScope.SERVERS_WRITE in scopes
    assert TokenScope.ADMIN_WRITE not in scopes
    with pytest.raises(ValueError):
        await exchange_pairing(container, code, uuid4())
    tokens.create.assert_awaited_once()


async def test_pairing_rechecks_account_after_issuance(pairing):
    user, container, tokens = pairing
    code = await issue_pairing(container, user)
    user.status = UserStatus.BANNED
    with pytest.raises(ValueError):
        await exchange_pairing(container, code, uuid4())
    tokens.create.assert_not_awaited()


async def test_pairing_scopes_only_verified_superadmins(pairing, monkeypatch):
    user, container, tokens = pairing
    user.role = Role.ADMIN
    import cloud_platform.modules.tokens.website as module

    monkeypatch.setattr(
        module, "get_settings", lambda: SimpleNamespace(telegram_superadmin_user_ids=[42])
    )
    await exchange_pairing(container, await issue_pairing(container, user), uuid4())
    assert TokenScope.ADMIN_WRITE in tokens.create.call_args.kwargs["scopes"]


async def test_shared_fixture_requires_current_superadmin_even_for_owner(monkeypatch):
    owner = customer()
    server = fixture_server(owner.id)
    container = SimpleNamespace(
        server_repository=lambda: SimpleNamespace(get=AsyncMock(return_value=server))
    )
    monkeypatch.setattr(
        console, "get_settings", lambda: SimpleNamespace(telegram_superadmin_user_ids=[42])
    )
    with pytest.raises(ApiError):
        await console.target_owner(container, owner, server.id)
    owner.role = Role.ADMIN
    assert await console.target_owner(container, owner, server.id) == owner.id
    stranger = customer()
    with pytest.raises(ApiError):
        await console.target_owner(container, stranger, server.id)
    server.ipv4 = "192.0.2.100"
    assert not is_test_server(server)
    with pytest.raises(ApiError):
        await console.target_owner(container, owner, server.id)
    owner.role = Role.USER
    with pytest.raises(ApiError):
        await console.target_owner(container, owner, server.id)


class ReceiptRepository:
    def __init__(self):
        self.operation = None
        self.operations = {}

    async def management_intent(self, *, key, server_id, provider_key, digest):
        operation = self.operations.get(key)
        if operation is None:
            operation = Operation(
                id=uuid4(),
                operation_key=key,
                operation_type=OperationType.SERVER_MANAGEMENT,
                resource_type="server",
                resource_id=server_id,
                provider_key=provider_key,
                provider_response={"command_digest": digest},
            )
            self.operations[key] = operation
        elif operation.provider_response["command_digest"] != digest:
            raise ValueError("mismatched command")
        self.operation = operation
        return deepcopy(operation)

    async def claim(self, identifier):
        operation = next(row for row in self.operations.values() if row.id == identifier)
        if operation.status is not OperationStatus.PENDING:
            return None
        operation.mark_in_flight()
        self.operation = operation
        return deepcopy(operation)

    async def save(self, operation):
        self.operation = deepcopy(operation)
        self.operations[operation.operation_key] = self.operation

    async def get_by_key(self, key):
        return deepcopy(self.operations.get(key))


async def test_receipt_concurrency_replay_and_payload_binding():
    repository = ReceiptRepository()
    server = fixture_server()
    user_id = uuid4()
    started, release = asyncio.Event(), asyncio.Event()

    async def dispatch():
        started.set()
        await release.wait()
        return ServerActionOutcome(operation=ServerOperation.REBOOT, accepted=True)

    kwargs = dict(
        key="request-1",
        user_id=user_id,
        server=server,
        action="reboot",
        arguments={},
        prepare=AsyncMock(return_value={"action": "reboot", "lifecycle": "running"}),
        dispatch=dispatch,
    )
    first = asyncio.create_task(execute_management(repository, **kwargs))
    await started.wait()
    with pytest.raises(ManagementReceiptConflict):
        await execute_management(repository, **kwargs)
    release.set()
    result = await first
    assert result["accepted"]
    replay = await execute_management(repository, **kwargs)
    assert replay["replayed"] is True
    with pytest.raises(ManagementReceiptConflict):
        await execute_management(repository, **(kwargs | {"action": "delete"}))


async def test_unknown_outcome_never_reexecutes_provider():
    repository = ReceiptRepository()
    dispatch = AsyncMock(side_effect=TimeoutError())
    kwargs = dict(
        key="lost-response",
        user_id=uuid4(),
        server=fixture_server(),
        action="reinstall",
        arguments={"image": "fixture"},
        prepare=AsyncMock(return_value={"action": "reinstall", "lifecycle": "running"}),
        dispatch=dispatch,
    )
    with pytest.raises(TimeoutError):
        await execute_management(repository, **kwargs)
    assert repository.operation.status is OperationStatus.OUTCOME_UNKNOWN
    with pytest.raises(ManagementReceiptConflict):
        await execute_management(repository, **kwargs)
    dispatch.assert_awaited_once()


async def test_notices_work_with_operator_channel_disabled_and_exclude_secrets():
    repository = SimpleNamespace(enqueue=AsyncMock(return_value=True))
    sink = CustomerEventSink(repository, BusinessLogPolicy(enabled=False))
    user = customer()
    await sink.emit(
        BusinessEvent(
            event_key="fixture-event",
            event_type=BusinessEventType.SERVER_STOPPED,
            payload={
                "user_id": str(user.id),
                "server_id": str(uuid4()),
                "password": "never-render-this",  # pragma: allowlist secret
                "provider_error": "private diagnostic",
            },
        )
    )
    repository.enqueue.assert_awaited_once()
    values = repository.enqueue.call_args.kwargs
    assert values["event_type"] == "customer.notice"
    assert values["payload"]["user_id"] == str(user.id)
    assert "never-render-this" not in str(values) and "private diagnostic" not in str(values)
    row = notice_row(key="ledger:1", user_id=user.id, text="موجودی تغییر کرد")
    assert row.event_type == "customer.notice" and row.status == "PENDING"


async def test_console_http_scope_idempotency_and_validation(monkeypatch):
    monkeypatch.setattr(console, "get_settings", lambda: SimpleNamespace(app_env="staging"))
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(console.router)
    auth = TokenAuthentication(
        user_id=uuid4(), token_id=uuid4(), scopes=frozenset({TokenScope.SERVERS_READ})
    )
    app.state.authenticate_token = AsyncMock(return_value=auth)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        client.headers.update({"X-Platform-Contract": "1", "X-Platform-Environment": "staging"})
        response = await client.get("/v1/console/account")
        assert response.status_code == 401
        response = await client.post(
            f"/v1/console/servers/{uuid4()}/actions/reboot",
            headers={"Authorization": "Bearer cpt_fixture", "Idempotency-Key": "fixture-key"},
            json={},
        )
        assert response.status_code == 403
        app.state.authenticate_token.return_value = TokenAuthentication(
            user_id=auth.user_id, token_id=auth.token_id, scopes=frozenset(TokenScope)
        )
        response = await client.post(
            "/v1/console/orders",
            headers={"Authorization": "Bearer cpt_fixture", "Idempotency-Key": "fixture-key"},
            json={
                "offerId": "not-a-uuid",
                "image": "fixture",
                "expectedMinor": True,
                "expectedCurrency": "IRT",
            },
        )
        assert response.status_code == 400
        response = await client.post(
            f"/v1/console/servers/{uuid4()}/actions/reboot",
            headers={"Authorization": "Bearer cpt_fixture"},
            json={},
        )
        assert response.status_code == 428


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-Platform-Contract": "1", "X-Platform-Environment": "production"},
        {"X-Platform-Contract": "2", "X-Platform-Environment": "staging"},
    ],
)
async def test_cross_environment_or_incompatible_link_cannot_consume_pairing(headers, monkeypatch):
    monkeypatch.setattr(console, "get_settings", lambda: SimpleNamespace(app_env="staging"))
    exchange = AsyncMock()
    import cloud_platform.modules.tokens.website as pairing_module

    monkeypatch.setattr(pairing_module, "exchange_pairing", exchange)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(console.link_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            "/v1/console/link/exchange",
            headers=headers,
            json={"code": "cpl_" + "a" * 43, "websiteUserId": str(uuid4())},
        )
    assert response.status_code == 409
    exchange.assert_not_awaited()


async def test_ssh_alias_pinning_confirmation_and_reachability(monkeypatch, tmp_path):
    actor, server = customer(role=Role.ADMIN), fixture_server()
    monkeypatch.setattr(
        guest,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_superadmin_user_ids=[42], test_server_ssh_alias="test-vps"
        ),
    )
    config = tmp_path / "config"
    config.write_text(f"Host test-vps\n  HostName {TEST_IPV4}\n")
    monkeypatch.setattr(guest, "SSH_CONFIG_PATH", config)
    verifier = SimpleNamespace(consume=AsyncMock(return_value=SimpleNamespace(ok=True)))
    container = SimpleNamespace(confirmation_verifier=lambda: verifier)
    ssh = AsyncMock(side_effect=[f"hostname {TEST_IPV4}\n".encode(), b"", b""])
    monkeypatch.setattr(guest, "_ssh", ssh)
    result = await guest.change_test_ssh_port(
        container, actor, server, 2222, "fixture-token", "key"
    )
    assert result.accepted
    with pytest.raises(ValueError):
        guest.port_binding(actor, server, True)
    ssh.reset_mock(side_effect=True)
    ssh.side_effect = [b"hostname 192.0.2.1\n"]
    verifier.consume.reset_mock()
    with pytest.raises(ValueError):
        await guest.change_test_ssh_port(container, actor, server, 2222, "fixture-token", "key")
    verifier.consume.assert_not_awaited()
    ssh.assert_awaited_once()
    config.unlink()
    with pytest.raises(ValueError):
        guest.port_binding(actor, server, 2222)


async def test_customer_dispatch_uses_persisted_recipient_and_bounded_retry():
    user = customer()
    record = SimpleNamespace(
        event_key="customer:fixture",
        event_type="customer.notice",
        attempts=1,
        payload={"user_id": str(user.id), "text": "عملیات ثبت شد", "chat_id": 999},
    )
    repository = SimpleNamespace(
        claim_due=AsyncMock(return_value=[record]),
        mark_sent=AsyncMock(),
        mark_retry=AsyncMock(),
        mark_abandoned=AsyncMock(),
    )
    channel = SimpleNamespace(send=AsyncMock(), send_customer=AsyncMock())
    users = SimpleNamespace(get=AsyncMock(return_value=user))
    dispatcher = BusinessLogDispatcher(
        repository, channel, BusinessLogPolicy(enabled=False), user_repo=users
    )
    report = await dispatcher.deliver()
    assert report.sent == 1
    assert repository.claim_due.call_args.kwargs["customer_only"] is True
    channel.send_customer.assert_awaited_once_with(42, "عملیات ثبت شد")
    channel.send.assert_not_awaited()
    channel.send_customer.side_effect = RuntimeError("transport unavailable")
    report = await dispatcher.deliver()
    assert report.retried == 1
    repository.mark_retry.assert_awaited_once()


async def test_pairing_bot_ignores_group_and_mismatched_private_chat(pairing):
    from aiogram import Dispatcher

    from cloud_platform.bot.platform_bridge import register_bridge_handlers

    user, container, _ = pairing
    dispatcher = Dispatcher()
    resolve = AsyncMock(return_value=user)
    register_bridge_handlers(dispatcher, container, resolve)
    website = next(
        h.callback for h in dispatcher.message.handlers if h.callback.__name__ == "website"
    )
    answer = AsyncMock()
    message = SimpleNamespace(
        chat=SimpleNamespace(type="group", id=-100), from_user=SimpleNamespace(id=42), answer=answer
    )
    await website(message)
    message.chat = SimpleNamespace(type="private", id=43)
    await website(message)
    resolve.assert_not_awaited()
    answer.assert_not_awaited()
    message.chat.id = 42
    await website(message)
    resolve.assert_awaited_once()
    assert "cpl_" in answer.call_args.args[0]
