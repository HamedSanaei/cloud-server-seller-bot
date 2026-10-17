"""Opt-in bridge invariants on disposable PostgreSQL schemas, never customer rows."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import MetaData, delete, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import DefaultClause

from cloud_platform.core.secrets import FernetSecretBox, MasterKey, SecretEnvelope
from cloud_platform.db.base import (
    AuditEvent,
    Base,
    BusinessLogEvent,
    Operation,
    Provider,
    ProviderAccount,
    Server,
    ServerCreateCredential,
    TelegramSuperadminAssignment,
    User,
)
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
from cloud_platform.modules.hourly.credentials import SqlAlchemyServerCredentialStore
from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
from cloud_platform.modules.servers import management_lifecycle as lifecycle
from cloud_platform.modules.servers.commands import execute_management
from cloud_platform.modules.servers.models import ServerActionOutcome, ServerOperation
from cloud_platform.modules.servers.test_identity import TEST_IPV4, TEST_PROVIDER_ID
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository
from cloud_platform.providers.vps_ports import VpsInfo, VpsOneTimePassword, VpsPasswordIssued

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(not DB_URL, reason="requires explicit scratch PostgreSQL URL")


@pytest.fixture
async def database():
    url = make_url(DB_URL)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Bridge contracts only accept explicit scratch PostgreSQL on loopback")
    schema = "website_bridge_test_" + uuid4().hex
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "5000",
                "timezone": "UTC",
            }
        },
    )
    subset = MetaData()
    for source in Base.metadata.sorted_tables:
        table = source.to_metadata(subset)
        for column in table.columns:
            default = column.server_default
            if default is not None and "uuid_generate_v4" in str(default.arg):
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif default is not None and str(default.arg).upper() == "CURRENT_TIMESTAMP":
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(subset.create_all)
        created = True
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await engine.dispose()


@pytest.fixture
async def setup(database, monkeypatch):
    owner, provider_id, account, server_id = uuid4(), uuid4(), uuid4(), uuid4()
    async with database.begin() as session:
        session.add(
            User(
                id=owner,
                username="bridge-fixture",
                email="bridge@example.test",
                role="admin",
                status="active",
                telegram_user_id=42,
            )
        )
        session.add(Provider(id=provider_id, name="leaseweb"))
        await session.flush()
        session.add(TelegramSuperadminAssignment(telegram_user_id=42, enabled=True, user_id=owner))
        session.add(ProviderAccount(id=account, provider_id=provider_id, user_id=owner))
        await session.flush()
        session.add(
            Server(
                id=server_id,
                user_id=owner,
                provider_id=provider_id,
                provider_account_id=account,
                provider_server_id=TEST_PROVIDER_ID,
                ipv4=TEST_IPV4,
                state="running",
                credential_account_id="verified-account",
                price_per_quantum=0,
                currency="IRT",
                billing_model="prepaid_monthly_fixed",
                funding_source="operator",
                funding_operator_id=owner,
            )
        )
    servers = SqlAlchemyServerRepository(database)
    server = await servers.get(server_id)
    box = FernetSecretBox(MasterKey.generate())
    store = SqlAlchemyServerCredentialStore(database, box=box)
    monkeypatch.setattr(
        lifecycle,
        "get_settings",
        lambda: SimpleNamespace(callback_signing_key="test-only-signing-key-" * 3),
    )

    class ProviderState:
        info = VpsInfo(
            id=TEST_PROVIDER_ID, state="RUNNING", started_at="initial-boot", image_id="old-image"
        )
        password = "old-test-password"  # pragma: allowlist secret
        dispatches = 0
        authoritative_vps_inventory = True
        from cloud_platform.providers.base import Capability

        capabilities = frozenset({Capability.POWER})

        async def list_vps_info(self):
            return [self.info]

        async def start_vps(self, _identifier):
            self.dispatches += 1

        async def stop_vps(self, _identifier):
            self.dispatches += 1

        async def reboot_vps(self, _identifier):
            self.dispatches += 1

        async def power_on(self, identifier, _key):
            await self.start_vps(identifier)

        async def power_off(self, identifier, _key):
            await self.stop_vps(identifier)

        async def reboot(self, identifier, _key):
            await self.reboot_vps(identifier)

        async def list_vps_reinstall_images(self, _identifier):
            from cloud_platform.providers.vps_ports import VpsReinstallImage

            return [VpsReinstallImage(id="new-image", name="New test OS")]

        async def reinstall_vps(self, _identifier, _image):
            self.dispatches += 1

        async def reset_vps_password(self, _identifier):
            self.dispatches += 1
            return None

        async def get_vps_info(self, _identifier):
            return self.info

        async def list_vps_ips(self, _identifier):
            return [SimpleNamespace(ip=TEST_IPV4)]

        async def get_vps_root_password(self, _identifier):
            return VpsPasswordIssued(
                provider_server_id=TEST_PROVIDER_ID,
                action="reset-password",
                root_password=VpsOneTimePassword(self.password),
                username="Administrator",
            )

        async def dispatch(self):
            self.dispatches += 1
            return ServerActionOutcome(operation=ServerOperation.STOP, accepted=True)

    provider = ProviderState()

    class Registry:
        def get_for(self, key, account_id):
            if (key, account_id) != ("leaseweb", "verified-account"):
                raise KeyError("wrong credential account")
            return provider

    registry = Registry()
    container = SimpleNamespace(
        session_factory=database,
        provider_registry=registry,
        server_repository=lambda: servers,
        server_credential_store=lambda: store,
        user_repository=lambda: SqlAlchemyUserRepository(database),
    )
    return SimpleNamespace(
        database=database,
        container=container,
        server=server,
        provider=provider,
        operations=SqlAlchemyOperationRepository(database),
        owner=owner,
        box=box,
    )


async def submit(setup, *, key="power-command", action="power-off"):
    return await execute_management(
        setup.operations,
        key=key,
        user_id=setup.owner,
        server=setup.server,
        action=action,
        arguments={},
        prepare=lambda: lifecycle.prepare_observation(
            setup.container.provider_registry, setup.server, setup.owner, action, {}
        ),
        dispatch=setup.provider.dispatch,
    )


async def test_distinct_concurrent_keys_cannot_both_dispatch_one_resource(setup):
    async def intent(key):
        return await setup.operations.management_intent(
            key=key,
            server_id=setup.server.id,
            provider_key="leaseweb",
            digest=key,
        )

    results = await asyncio.gather(intent("command-a"), intent("command-b"), return_exceptions=True)
    assert sum(isinstance(result, ValueError) for result in results) == 1
    async with setup.database() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 1


async def test_http_acceptance_needs_provider_proof_and_concurrent_observers_notify_once(setup):
    result = await submit(setup)
    assert result["status"] == "running"
    assert await lifecycle.reconcile_management(setup.container) == 0
    async with setup.database() as session:
        assert await session.scalar(select(func.count()).select_from(BusinessLogEvent)) == 0
    setup.provider.info = replace(setup.provider.info, state="STOPPED")
    await asyncio.gather(
        lifecycle.reconcile_management(setup.container),
        lifecycle.reconcile_management(setup.container),
    )
    replay = await submit(setup)
    assert replay["status"] == "success" and replay["replayed"] is True
    assert setup.provider.dispatches == 1
    async with setup.database() as session:
        row = (await session.execute(select(Operation))).scalar_one()
        assert lifecycle.public_receipt(row)["observedState"] == "STOPPED"
        assert await session.scalar(select(func.count()).select_from(BusinessLogEvent)) == 1
    next_intent = await setup.operations.management_intent(
        key="next-safe-command",
        server_id=setup.server.id,
        provider_key="leaseweb",
        digest="next",
    )
    assert next_intent.status.value == "pending"


async def test_password_result_secret_and_notice_commit_atomically(setup):
    await submit(setup, action="reset-password", key="password-command")
    assert (
        await lifecycle.reconcile_management(setup.container) == 0
    )  # old provider password is not a reset result
    setup.provider.password = "new-test-password"  # pragma: allowlist secret
    async with setup.database.begin() as session:
        await session.execute(
            text(
                "CREATE FUNCTION reject_notice() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'injected notice commit failure'; END; $$"
            )
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_notice BEFORE INSERT ON business_log_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_notice()"
            )
        )
    with pytest.raises(ExceptionGroup):
        await lifecycle.reconcile_management(setup.container)
    async with setup.database() as session:
        assert await session.get(ServerCreateCredential, setup.server.id) is None
        row = (await session.execute(select(Operation))).scalar_one()
        assert lifecycle.public_receipt(row)["status"] == "running"
    async with setup.database.begin() as session:
        await session.execute(text("DROP TRIGGER reject_notice ON business_log_events"))
    assert await lifecycle.reconcile_management(setup.container) == 1
    async with setup.database() as session:
        credential = await session.get(ServerCreateCredential, setup.server.id)
        assert credential.username == "Administrator"
        assert (
            setup.box.decrypt(
                SecretEnvelope(
                    token=credential.ciphertext,
                    key_id=credential.key_id,
                    algorithm=credential.algorithm,
                )
            )
            == setup.provider.password
        )
        row = (await session.execute(select(Operation))).scalar_one()
        assert lifecycle.public_receipt(row)["status"] == "success"
        assert setup.provider.password not in str(row.provider_response)
        notices = (await session.execute(select(BusinessLogEvent))).scalars().all()
        assert len(notices) == 1 and setup.provider.password not in str(notices[0].payload)


async def test_reserved_resource_never_reappears_in_customer_list_after_metadata_drift(setup):
    async with setup.database.begin() as session:
        await session.execute(
            update(Server).where(Server.id == setup.server.id).values(ipv4="192.0.2.1")
        )
    rows, count = await setup.container.server_repository().list_by_user_paged(
        setup.owner,
        offset=0,
        limit=10,
    )
    assert rows == [] and count == 0


@pytest.mark.parametrize("missing_provider", [False, True])
async def test_concurrent_registration_keeps_one_asset_owner_and_one_import_receipt(
    setup, monkeypatch, missing_provider
):
    from cloud_platform.modules.servers import test_fixture

    monkeypatch.setattr(
        test_fixture,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_superadmin_user_ids=[42],
            leaseweb_managed_accounts=[SimpleNamespace(id="verified-account")],
        ),
    )
    async with setup.database.begin() as session:
        await session.execute(delete(Server).where(Server.id == setup.server.id))
        if missing_provider:
            await session.execute(delete(ProviderAccount))
            await session.execute(delete(Provider))
    first, second = await asyncio.gather(
        test_fixture.register_test_server(setup.container),
        test_fixture.register_test_server(setup.container),
    )
    assert first.id == second.id
    assert first.user_id == first.funding_operator_id == setup.owner
    assert first.credential_account_id == "verified-account"
    assert setup.provider.dispatches == 0
    async with setup.database() as session:
        assert await session.scalar(select(func.count()).select_from(Server)) == 1
        assert await session.scalar(select(func.count()).select_from(AuditEvent)) == 1


async def test_rejected_provider_request_has_failed_safe_receipt_without_secret_diagnostics(setup):
    from cloud_platform.modules.servers.service import ServerUnavailableError

    async def rejected():
        raise ServerUnavailableError("private-provider-diagnostic")

    with pytest.raises(ServerUnavailableError):
        await execute_management(
            setup.operations,
            key="rejected-command",
            user_id=setup.owner,
            server=setup.server,
            action="power-off",
            arguments={},
            prepare=lambda: lifecycle.prepare_observation(
                setup.container.provider_registry, setup.server, setup.owner, "power-off", {}
            ),
            dispatch=rejected,
        )
    async with setup.database() as session:
        row = (await session.execute(select(Operation))).scalar_one()
        receipt = lifecycle.public_receipt(row)
        assert receipt["status"] == "failed"
        assert receipt["detail"] == "request_rejected:ServerUnavailableError"
        assert "private-provider-diagnostic" not in str(row.provider_response) + str(row.error)


@pytest.fixture
async def console_runtime(setup, monkeypatch, tmp_path):
    import base64

    import httpx
    from fastapi import FastAPI

    from cloud_platform.api.v1 import console
    from cloud_platform.api.v1.errors import install_error_handlers
    from cloud_platform.core.config import get_settings
    from cloud_platform.core.container import Container
    from cloud_platform.core.session_store import InMemoryBotSessionStore
    from cloud_platform.db.base import Wallet

    config = tmp_path / "isolated.toml"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("CLOUD_PLATFORM_CONFIG_FILE", str(config))
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("API_ALLOW_HEADER_IDENTITY", "false")
    monkeypatch.setenv("TELEGRAM_SUPERADMIN_USER_IDS", "[42,43]")
    monkeypatch.setenv("CALLBACK_SIGNING_KEY", "test-only-signing-key-" * 3)
    monkeypatch.setenv(
        "PROVIDER_CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode()
    )
    get_settings.cache_clear()
    customer, peer = uuid4(), uuid4()
    async with setup.database.begin() as session:
        session.add_all(
            [
                User(
                    id=customer,
                    username="customer",
                    email="customer@example.test",
                    telegram_user_id=44,
                    status="active",
                    role="user",
                ),
                User(
                    id=peer,
                    username="peer-admin",
                    email="peer@example.test",
                    telegram_user_id=43,
                    status="active",
                    role="admin",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Wallet(user_id=setup.owner, balance=0, currency="IRT"),
                Wallet(user_id=customer, balance=0, currency="IRT"),
                Wallet(user_id=peer, balance=0, currency="IRT"),
            ]
        )
        session.add(TelegramSuperadminAssignment(telegram_user_id=43, enabled=True, user_id=peer))
    runtime = Container(
        session_factory=setup.database,
        provider_registry=setup.container.provider_registry,
        provider_allocator=None,
        hetzner_syncer=None,
        bot_session_store=InMemoryBotSessionStore(),
    )

    async def resolve_container():
        return runtime

    monkeypatch.setattr(console, "get_container", resolve_container)
    app = FastAPI()
    install_error_handlers(app)
    app.state.authenticate_token = runtime.token_service().authenticate
    app.include_router(console.link_router)
    app.include_router(console.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://console.test",
        headers={"X-Platform-Contract": "1", "X-Platform-Environment": "test"},
    ) as client:
        yield SimpleNamespace(
            **vars(setup), runtime=runtime, client=client, customer=customer, peer=peer
        )
    get_settings.cache_clear()


async def linked_token(context, user_id):
    from cloud_platform.modules.tokens.website import issue_pairing

    user = await context.runtime.user_repository().get(user_id)
    code = await issue_pairing(context.runtime, user)
    response = await context.client.post(
        "/v1/console/link/exchange", json={"code": code, "websiteUserId": str(uuid4())}
    )
    assert response.status_code == 200
    return response.json()["data"]["token"], code


def bearer(token, key=None):
    return {"Authorization": "Bearer " + token, **({"Idempotency-Key": key} if key else {})}


async def test_http_pairing_wallet_replay_and_recipient_isolation(console_runtime):
    from cloud_platform.db.base import ApiTokenRow, LedgerEntry, Wallet

    ctx = console_runtime
    admin, _ = await linked_token(ctx, ctx.owner)
    customer, used = await linked_token(ctx, ctx.customer)
    replay = await ctx.client.post(
        "/v1/console/link/exchange", json={"code": used, "websiteUserId": str(uuid4())}
    )
    assert replay.status_code == 401
    payload = {"amountMinor": 12345, "reason": "isolated funding proof"}
    path = f"/v1/console/admin/users/{ctx.customer}/wallet-adjustments"
    for _ in range(2):
        result = await ctx.client.post(path, headers=bearer(admin, "credit-once"), json=payload)
        assert result.status_code == 200 and result.json()["data"]["currency"] == "IRT"
    conflict = await ctx.client.post(
        path, headers=bearer(admin, "credit-once"), json={**payload, "amountMinor": 999}
    )
    assert conflict.status_code == 409
    denied = await ctx.client.post(path, headers=bearer(customer, "forged-credit"), json=payload)
    assert denied.status_code == 403
    wallet = await ctx.client.get("/v1/console/wallet", headers=bearer(customer))
    assert wallet.status_code == 200
    assert wallet.json()["data"]["balance"]["balance_minor"] == 12345
    assert (await ctx.runtime.wallet_history_service().balance(ctx.customer)).balance_minor == 12345
    notices = await ctx.client.get("/v1/console/notifications", headers=bearer(customer))
    other = await ctx.client.get("/v1/console/notifications", headers=bearer(admin))
    assert len(notices.json()["data"]) == 1 and other.json()["data"] == []
    async with ctx.database() as session:
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        row = await session.scalar(select(Wallet).where(Wallet.user_id == ctx.customer))
        assert row.balance == 12345 and row.currency == "IRT"
        tokens = (await session.execute(select(ApiTokenRow))).scalars().all()
        assert len(tokens) == 2 and all(
            token.token_hash not in {admin, customer} for token in tokens
        )


async def test_http_authority_scope_and_current_admin_role_are_rechecked(console_runtime):
    from cloud_platform.modules.tokens.domain import TokenScope

    ctx = console_runtime
    token, _ = await linked_token(ctx, ctx.owner)
    account = await ctx.client.get("/v1/console/account", headers=bearer(token))
    assert account.json()["data"]["superadmin"] is True
    wrong_environment = await ctx.client.get(
        "/v1/console/wallet", headers={**bearer(token), "X-Platform-Environment": "production"}
    )
    assert wrong_environment.status_code == 409
    _, limited = await ctx.runtime.token_service().create(
        actor_user_id=ctx.customer,
        owner_user_id=ctx.customer,
        name="read-only",
        scopes=frozenset({TokenScope.WALLET_READ}),
    )
    forbidden = await ctx.client.post(
        "/v1/console/recharge",
        headers=bearer(limited, "no-write"),
        json={"amountMinor": 1000, "gateway": "unconfigured"},
    )
    assert forbidden.status_code == 403
    async with ctx.database.begin() as session:
        await session.execute(update(User).where(User.id == ctx.owner).values(role="user"))
    stale = await ctx.client.get("/v1/console/admin/users", headers=bearer(token))
    assert stale.status_code == 403
    revoked = (
        await ctx.runtime.token_service().list_tokens(
            actor_user_id=ctx.owner, owner_user_id=ctx.owner
        )
    )[0]
    await ctx.runtime.token_service().revoke(actor_user_id=ctx.owner, token_id=revoked.id)
    invalid = await ctx.client.get("/v1/console/wallet", headers=bearer(token))
    assert invalid.status_code == 401


async def test_http_shared_fixture_visibility_and_current_provider_evidence(console_runtime):
    ctx = console_runtime
    admin, _ = await linked_token(ctx, ctx.owner)
    peer, _ = await linked_token(ctx, ctx.peer)
    customer, _ = await linked_token(ctx, ctx.customer)
    for token in (admin, peer):
        detail = await ctx.client.get(f"/v1/console/servers/{ctx.server.id}", headers=bearer(token))
        assert detail.status_code == 200
        assert detail.json()["data"]["server"]["ip"] == TEST_IPV4
        assert "delete" not in detail.json()["data"]["operations"]
        listing = await ctx.client.get("/v1/console/servers", headers=bearer(token))
        assert [row["server_id"] for row in listing.json()["data"]] == [str(ctx.server.id)]
    denied = await ctx.client.get(f"/v1/console/servers/{ctx.server.id}", headers=bearer(customer))
    assert denied.status_code == 404
    assert (await ctx.client.get("/v1/console/servers", headers=bearer(customer))).json()[
        "data"
    ] == []
    async with ctx.database() as session:
        stored = await session.get(Server, ctx.server.id)
        assert stored.user_id == ctx.owner and stored.funding_operator_id == ctx.owner
    assert ctx.provider.dispatches == 0


async def test_http_power_confirmation_reaches_authorized_executor_without_owner_transfer(
    console_runtime,
):
    ctx = console_runtime
    admin, _ = await linked_token(ctx, ctx.peer)
    path = f"/v1/console/servers/{ctx.server.id}"
    confirmation = await ctx.client.post(
        path + "/confirmations/power-off", headers=bearer(admin, "confirm-stop"), json={}
    )
    assert confirmation.status_code == 200
    body = {"confirmationToken": confirmation.json()["data"]["confirmationToken"]}
    response = await ctx.client.post(
        path + "/actions/power-off", headers=bearer(admin, "stop-once"), json=body
    )
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "running"
    assert ctx.provider.dispatches == 1
    assert await lifecycle.reconcile_management(ctx.runtime) == 0
    ctx.provider.info = replace(ctx.provider.info, state="STOPPED")
    assert await lifecycle.reconcile_management(ctx.runtime) == 1
    history = await ctx.client.get(path + "/operations", headers=bearer(admin))
    assert any(
        row["action"] == "power-off" and row["status"] == "success"
        for row in history.json()["data"]
    )
    replay = await ctx.client.post(
        path + "/actions/power-off", headers=bearer(admin, "stop-once"), json=body
    )
    assert replay.status_code == 202 and ctx.provider.dispatches == 1
    async with ctx.database() as session:
        stored = await session.get(Server, ctx.server.id)
        assert stored.user_id == ctx.owner and stored.funding_operator_id == ctx.owner
        privileged = await session.scalar(
            select(Operation).where(
                Operation.provider_response["admin_support"]["actor_id"].astext == str(ctx.peer)
            )
        )
        assert privileged is not None and privileged.resource_id == ctx.server.id
        assert privileged.provider_response["admin_support"]["actor_id"] != str(ctx.owner)


async def test_http_async_password_image_and_one_time_secret_require_observed_evidence(
    console_runtime,
):
    ctx = console_runtime
    admin, _ = await linked_token(ctx, ctx.peer)
    path = f"/v1/console/servers/{ctx.server.id}"

    async def confirmed(action, key, payload):
        confirmation = await ctx.client.post(
            path + "/confirmations/" + action,
            headers=bearer(admin, key + "-confirm"),
            json=payload,
        )
        assert confirmation.status_code == 200
        return await ctx.client.post(
            path + "/actions/" + action,
            headers=bearer(admin, key),
            json={**payload, "confirmationToken": confirmation.json()["data"]["confirmationToken"]},
        )

    reset = await confirmed("reset-password", "reset-once", {})
    assert reset.status_code == 202 and reset.json()["data"]["status"] == "running"
    unavailable = await ctx.client.post(
        path + "/credentials/claim", headers=bearer(admin, "claim-before-result")
    )
    assert unavailable.status_code == 404
    assert await lifecycle.reconcile_management(ctx.runtime) == 0
    ctx.provider.password = "replacement-" + ctx.provider.password  # pragma: allowlist secret
    assert await lifecycle.reconcile_management(ctx.runtime) == 1
    history = await ctx.client.get(path + "/operations", headers=bearer(admin))
    assert any(
        row["action"] == "reset-password" and row["status"] == "success"
        for row in history.json()["data"]
    )
    assert ctx.provider.password not in history.text
    claimed = await ctx.client.post(
        path + "/credentials/claim", headers=bearer(admin, "claim-after-result")
    )
    assert claimed.status_code == 200 and claimed.headers["Cache-Control"] == "no-store"
    assert claimed.json()["data"]["password"] == ctx.provider.password
    assert claimed.json()["data"]["username"] == "Administrator"
    acknowledged = await ctx.client.post(
        path + "/credentials/ack",
        headers=bearer(admin, "ack-private-result"),
        json={"claimId": claimed.json()["data"]["claimId"]},
    )
    assert acknowledged.json()["data"]["ok"] is True
    assert (
        await ctx.client.post(path + "/credentials/claim", headers=bearer(admin, "claim-again"))
    ).status_code == 404
    options = await ctx.client.get(path + "/images", headers=bearer(admin))
    assert options.status_code == 200
    image = options.json()["data"][0]["ref"]
    reinstall = await confirmed("reinstall", "image-once", {"image": image})
    assert reinstall.status_code == 202 and reinstall.json()["data"]["status"] == "running"
    assert await lifecycle.reconcile_management(ctx.runtime) == 0
    ctx.provider.info = replace(
        ctx.provider.info, image_id=image, started_at="replacement-boot", state="RUNNING"
    )
    assert await lifecycle.reconcile_management(ctx.runtime) == 1
    final = await ctx.client.get(path + "/operations", headers=bearer(admin))
    assert any(
        row["action"] == "reinstall" and row["status"] == "success" for row in final.json()["data"]
    )
    notices = await ctx.client.get("/v1/console/notifications", headers=bearer(admin))
    assert ctx.provider.password not in notices.text
    assert ctx.provider.dispatches == 2


@pytest.fixture
def telegram_ui(console_runtime, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from aiogram import Bot, Dispatcher
    from aiogram.types import CallbackQuery, Chat, Message, Update
    from aiogram.types import User as TelegramUser

    from cloud_platform.bot.platform_bridge import register_bridge_handlers

    ctx = console_runtime
    answer, ack = AsyncMock(), AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setattr(CallbackQuery, "answer", ack)

    async def resolve(_container, sender):
        return await ctx.runtime.user_repository().get(
            {42: ctx.owner, 43: ctx.peer, 44: ctx.customer}[sender.id]
        )

    dispatcher = Dispatcher()
    register_bridge_handlers(dispatcher, ctx.runtime, resolve)

    async def send(text, actor=42):
        await dispatcher.feed_update(
            MagicMock(spec=Bot),
            Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=1,
                    chat=Chat(id=actor, type="private"),
                    from_user=TelegramUser(id=actor, first_name="Test", is_bot=False),
                    text=text,
                ),
            ),
        )

    async def click(data, actor=42):
        await dispatcher.feed_update(
            MagicMock(spec=Bot),
            Update(
                update_id=2,
                callback_query=CallbackQuery(
                    id="fixture-command",
                    chat_instance="private",
                    data=data,
                    from_user=TelegramUser(id=actor, first_name="Test", is_bot=False),
                    message=Message(message_id=1, date=1, chat=Chat(id=actor, type="private")),
                ),
            ),
        )

    async def choice(action):
        from cloud_platform.core.session_store import NS_REFERENCE

        markup = answer.await_args.kwargs["reply_markup"]
        for row in markup.inline_keyboard:
            for button in row:
                stored = await ctx.runtime.session_store().get(NS_REFERENCE, button.callback_data)
                if stored["action"] == action:
                    return button.callback_data
        raise AssertionError("requested capability is missing")

    return SimpleNamespace(send=send, click=click, choice=choice, answer=answer, ack=ack)


async def test_telegram_power_hijack_replay_and_website_result_share_one_intent(
    console_runtime, telegram_ui
):
    ctx, bot = console_runtime, telegram_ui
    admin, _ = await linked_token(ctx, ctx.owner)
    await bot.send("/testserver")
    stop = await bot.choice("stop")
    await bot.click(stop, actor=43)
    async with ctx.database() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 0
    await bot.click(stop)
    confirm = await bot.choice("stop")
    await bot.click(confirm)
    assert ctx.provider.dispatches == 1
    await bot.click(confirm)
    assert ctx.provider.dispatches == 1
    before = await ctx.client.get(
        f"/v1/console/servers/{ctx.server.id}/operations", headers=bearer(admin)
    )
    assert any(
        row["status"] == "running" and row["action"] == "power-off" for row in before.json()["data"]
    )
    ctx.provider.info = replace(ctx.provider.info, state="STOPPED")
    assert await lifecycle.reconcile_management(ctx.runtime) == 1
    after = await ctx.client.get(
        f"/v1/console/servers/{ctx.server.id}/operations", headers=bearer(admin)
    )
    assert any(
        row["status"] == "success" and row["action"] == "power-off" for row in after.json()["data"]
    )
    await bot.send("/testserver")
    await bot.click(await bot.choice("history"))
    assert "success" in bot.answer.await_args.args[0]
    async with ctx.database() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Operation)
                .where(Operation.operation_type == "server_management")
            )
            == 1
        )
        assert (await session.get(Server, ctx.server.id)).user_id == ctx.owner


async def test_registered_bot_pairing_keeps_identity_unforgeable_and_banned_inbox_private(
    console_runtime, telegram_ui
):
    from cloud_platform.modules.notifications.outbox import notice_row

    ctx, bot = console_runtime, telegram_ui
    await bot.send("/website", actor=44)
    code = bot.answer.await_args.args[0].splitlines()[-1]
    exchanged = await ctx.client.post(
        "/v1/console/link/exchange", json={"code": code, "websiteUserId": str(uuid4())}
    )
    assert exchanged.status_code == 200
    assert exchanged.json()["data"]["id"] == str(ctx.customer)
    token = exchanged.json()["data"]["token"]
    forged = await ctx.client.get(
        "/v1/console/admin/users",
        headers={**bearer(token), "X-Platform-User": str(ctx.owner)},
    )
    assert forged.status_code == 403
    header_only = await ctx.client.get(
        "/v1/console/wallet", headers={"X-Platform-User": str(ctx.owner)}
    )
    assert header_only.status_code == 401
    own = notice_row(key="own-status", user_id=ctx.customer, text="Account restricted")
    foreign = notice_row(key="foreign-status", user_id=ctx.owner, text="Private operator event")
    async with ctx.database.begin() as session:
        await session.execute(update(User).where(User.id == ctx.customer).values(status="banned"))
        session.add_all([own, foreign])
    wallet = await ctx.client.get("/v1/console/wallet", headers=bearer(token))
    assert wallet.status_code == 403
    inbox = await ctx.client.get("/v1/console/notifications", headers=bearer(token))
    assert inbox.status_code == 200
    assert [item["id"] for item in inbox.json()["data"]] == [str(own.id)]


async def test_telegram_image_confirmation_records_actual_image_completion_in_website(
    console_runtime, telegram_ui
):
    ctx, bot = console_runtime, telegram_ui
    admin, _ = await linked_token(ctx, ctx.owner)
    await bot.send("/testserver")
    await bot.click(await bot.choice("images"))
    await bot.click(await bot.choice("reinstall"))
    assert ctx.provider.dispatches == 0
    confirm = await bot.choice("reinstall")
    await bot.click(confirm)
    assert ctx.provider.dispatches == 1
    await bot.click(confirm)
    assert ctx.provider.dispatches == 1
    assert await lifecycle.reconcile_management(ctx.runtime) == 0
    ctx.provider.info = replace(
        ctx.provider.info, image_id="new-image", started_at="installed-boot", state="RUNNING"
    )
    assert await lifecycle.reconcile_management(ctx.runtime) == 1
    result = await ctx.client.get(
        f"/v1/console/servers/{ctx.server.id}/operations", headers=bearer(admin)
    )
    assert any(
        row["action"] == "reinstall" and row["status"] == "success" for row in result.json()["data"]
    )
    async with ctx.database() as session:
        operation = await session.scalar(
            select(Operation).where(Operation.operation_type == "server_management")
        )
        assert operation.provider_response["management"]["image"] == "new-image"
        assert (await session.get(Server, ctx.server.id)).user_id == ctx.owner


async def test_missing_guest_setup_rejects_both_port_interfaces_without_queued_mutation(
    console_runtime, telegram_ui
):
    ctx, bot = console_runtime, telegram_ui
    admin, _ = await linked_token(ctx, ctx.owner)
    path = f"/v1/console/servers/{ctx.server.id}/confirmations/ssh-port"
    malformed = await ctx.client.post(
        path, headers=bearer(admin, "invalid-port"), json={"port": True}
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "validation_error"
    unavailable = await ctx.client.post(
        path, headers=bearer(admin, "unconfigured-port"), json={"port": 2222}
    )
    assert unavailable.status_code == 409
    await bot.send("/testport 2222")
    assert "reply_markup" not in bot.answer.await_args.kwargs
    await bot.send("/testport 80")
    assert "reply_markup" not in bot.answer.await_args.kwargs
    async with ctx.database() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 0
    assert ctx.provider.dispatches == 0


async def test_catalog_stale_or_unpriced_offers_cannot_reserve_funds(console_runtime, monkeypatch):
    from cloud_platform.core.config import get_settings
    from cloud_platform.db.base import Hold, LedgerEntry, SellableOffer, Wallet

    ctx = console_runtime
    monkeypatch.setenv("FX_ENABLED", "false")
    monkeypatch.setenv("FX_CATALOG_PRICING_CURRENCY", "USD")
    get_settings.cache_clear()
    offer_id = uuid4()
    async with ctx.database.begin() as session:
        session.add(
            SellableOffer(
                id=offer_id,
                provider_key="leaseweb",
                product_id="catalog-only",
                location_id="TEST-01",
                name="USD internal price",
                vcpu=2,
                ram_gb=4,
                disk_gb=40,
                provider_cost_minor=100,
                provider_cost_currency="USD",
                selling_price_minor=250,
                selling_currency="USD",
                auto_priced=False,
                billing_parameters={"provider_monthly_rate": "1.00"},
                enabled=True,
                provider_available=True,
                provider_account_id="verified-account",
            )
        )
    admin, _ = await linked_token(ctx, ctx.owner)
    customer, _ = await linked_token(ctx, ctx.customer)
    credit = await ctx.client.post(
        f"/v1/console/admin/users/{ctx.customer}/wallet-adjustments",
        headers=bearer(admin, "catalog-funding"),
        json={"amountMinor": 50000, "reason": "isolated catalog funding"},
    )
    assert credit.status_code == 200
    catalog = await ctx.client.get("/v1/console/offers", headers=bearer(customer))
    assert catalog.status_code == 200
    row = next(item for item in catalog.json()["data"] if item["id"] == str(offer_id))
    assert row["displayCurrency"] == "IRT" and row["displayPriceMinor"] is None
    order = {
        "offerId": str(offer_id),
        "image": "new-image",
        "expectedMinor": 250,
        "expectedCurrency": "USD",
    }
    purchase = await ctx.client.post(
        "/v1/console/orders",
        headers=bearer(customer, "ineligible-purchase"),
        json=order,
    )
    assert purchase.status_code == 403
    async with ctx.database.begin() as session:
        await session.execute(
            update(SellableOffer).where(SellableOffer.id == offer_id).values(enabled=False)
        )
    withdrawn = await ctx.client.get(
        f"/v1/console/offers/{offer_id}/images", headers=bearer(customer)
    )
    assert withdrawn.status_code == 404
    stale_order = await ctx.client.post(
        "/v1/console/orders",
        headers=bearer(customer, "withdrawn-purchase"),
        json=order,
    )
    assert stale_order.status_code == 404
    async with ctx.database() as session:
        assert (
            await session.scalar(select(Wallet).where(Wallet.user_id == ctx.customer))
        ).balance == 50000
        assert await session.scalar(select(func.count()).select_from(Hold)) == 0
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(Server)) == 1
    assert ctx.provider.dispatches == 0


async def test_unavailable_gateway_cannot_create_payment_or_wallet_credit(console_runtime):
    from cloud_platform.db.base import LedgerEntry, PaymentSession, Wallet

    ctx = console_runtime
    customer, _ = await linked_token(ctx, ctx.customer)
    gateways = await ctx.client.get("/v1/console/recharge/gateways", headers=bearer(customer))
    assert gateways.status_code == 200 and gateways.json()["data"] == []
    result = await ctx.client.post(
        "/v1/console/recharge",
        headers=bearer(customer, "unconfigured-payment"),
        json={"amountMinor": 2000, "gateway": "absent-gateway"},
    )
    assert result.status_code == 409
    async with ctx.database() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentSession)) == 0
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
        assert (
            await session.scalar(select(Wallet).where(Wallet.user_id == ctx.customer))
        ).balance == 0


async def test_registration_rejects_conflicting_customer_asset_without_transfer(
    console_runtime, monkeypatch
):
    from cloud_platform.modules.servers import test_fixture

    ctx = console_runtime
    monkeypatch.setattr(
        test_fixture,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_superadmin_user_ids=[42, 43],
            leaseweb_managed_accounts=[SimpleNamespace(id="verified-account")],
        ),
    )
    admin, _ = await linked_token(ctx, ctx.owner)
    async with ctx.database.begin() as session:
        await session.execute(
            update(Server)
            .where(Server.id == ctx.server.id)
            .values(
                user_id=ctx.customer, funding_source="customer_wallet", funding_operator_id=None
            )
        )
    rejected = await ctx.client.post(
        "/v1/console/admin/test-server/register",
        headers=bearer(admin, "conflicting-fixture"),
        json={},
    )
    assert rejected.status_code == 409
    async with ctx.database() as session:
        stored = await session.get(Server, ctx.server.id)
        assert stored.user_id == ctx.customer and stored.funding_source == "customer_wallet"
        assert await session.scalar(select(func.count()).select_from(Server)) == 1
    assert ctx.provider.dispatches == 0


async def test_telegram_private_secret_delivery_failure_releases_claim_then_ack_consumes_it(
    console_runtime, telegram_ui
):
    ctx, bot = console_runtime, telegram_ui
    secret_value = uuid4().hex
    await ctx.runtime.server_credential_store().replace_issued(
        server_id=ctx.server.id,
        provider_server_id=TEST_PROVIDER_ID,
        password=secret_value,
        username="Administrator",
    )
    await bot.send("/testserver")
    request = await bot.choice("credentials")
    bot.answer.side_effect = RuntimeError("isolated transport failure")
    await bot.click(request)
    bot.answer.side_effect = None
    await bot.send("/testserver")
    await bot.click(await bot.choice("credentials"))
    delivered = bot.answer.await_args
    assert secret_value in delivered.args[0] and delivered.kwargs["protect_content"] is True
    assert (
        await ctx.runtime.server_management_service(actor_user_id=ctx.owner).claim_ssh_password(
            ctx.owner, ctx.server.id
        )
        is None
    )
    await bot.send("/testserver")
    await bot.click(await bot.choice("credentials"))
    assert secret_value not in bot.answer.await_args.args[0]
    async with ctx.database() as session:
        records = (await session.execute(select(BusinessLogEvent))).scalars().all()
        assert all(secret_value not in str(record.payload) for record in records)
    assert ctx.provider.dispatches == 0


async def test_http_pending_topup_replay_credits_toman_only_after_verified_callback(
    console_runtime, monkeypatch
):
    from cloud_platform.core.container import Container
    from cloud_platform.db.base import LedgerEntry, PaymentSession, Wallet
    from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
    from cloud_platform.modules.payments.service import PaymentWebhookService, WebhookAction
    from cloud_platform.providers.base import PaymentIntent, PaymentStatus
    from tests.unit.test_wallet_recharge import FakeGateway

    class Gateway(FakeGateway):
        supported_currency = "IRT"
        requires_durable_creation = True

        async def create_payment(self, *, amount_minor, currency, **kwargs):
            result = await super().create_payment(
                amount_minor=amount_minor, currency=currency, **kwargs
            )
            return PaymentIntent(
                gateway_payment_id=result.gateway_payment_id,
                status=PaymentStatus.PENDING,
                amount_minor=amount_minor,
                currency=currency,
                redirect_url=result.redirect_url,
                metadata={},
            )

    ctx = console_runtime
    gateway = Gateway(authority="isolated-" + uuid4().hex)
    monkeypatch.setattr(Container, "payment_gateways", lambda _self: {gateway.key: gateway})
    customer, _ = await linked_token(ctx, ctx.customer)
    payload = {"amountMinor": 2000, "gateway": gateway.key}
    for _ in range(2):
        created = await ctx.client.post(
            "/v1/console/recharge", headers=bearer(customer, "pending-credit-once"), json=payload
        )
        assert created.status_code == 202
    assert len(gateway.calls) == 1
    async with ctx.database() as session:
        invoice = await session.scalar(select(PaymentSession))
        assert invoice.status == "pending" and invoice.currency == "IRT"
        assert invoice.user_id == ctx.customer and invoice.amount_minor == 2000
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
        assert (
            await session.scalar(select(Wallet).where(Wallet.user_id == ctx.customer))
        ).balance == 0
    verified = PaymentWebhookService(
        SqlAlchemyPaymentSessionRepository(ctx.database),
        ctx.runtime.wallet_repository(),
        ctx.runtime.ledger_repository(),
        event_sink=ctx.runtime.business_event_sink(),
        user_repo=ctx.runtime.user_repository(),
    )
    args = dict(
        gateway_key=gateway.key,
        external_id=gateway.authority,
        status="succeeded",
        user_id=ctx.customer,
        amount_minor=2000,
        currency="IRT",
    )
    assert (await verified.process_callback(**args)).action is WebhookAction.CREDITED
    assert (await verified.process_callback(**args)).action is WebhookAction.DUPLICATE_IGNORED
    balance = await ctx.client.get("/v1/console/wallet", headers=bearer(customer))
    assert balance.json()["data"]["balance"]["balance_minor"] == 2000
    assert (await ctx.runtime.wallet_history_service().balance(ctx.customer)).balance_minor == 2000
    async with ctx.database() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentSession)) == 1
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 1


async def test_customer_prepaid_server_power_keeps_ownership_and_other_accounts_out(
    console_runtime,
):
    ctx = console_runtime
    server_id, account_id = uuid4(), uuid4()
    async with ctx.database.begin() as session:
        template = await session.get(Server, ctx.server.id)
        session.add(
            ProviderAccount(id=account_id, provider_id=template.provider_id, user_id=ctx.customer)
        )
        await session.flush()
        session.add(
            Server(
                id=server_id,
                user_id=ctx.customer,
                provider_id=template.provider_id,
                provider_account_id=account_id,
                credential_account_id="verified-account",
                provider_server_id="ordinary-instance",
                ipv4="192.0.2.44",
                state="stopped",
                price_per_quantum=0,
                currency="IRT",
                billing_model="prepaid_monthly_fixed",
                funding_source="customer_wallet",
            )
        )
    ctx.provider.info = replace(ctx.provider.info, id="ordinary-instance", state="STOPPED")
    customer, _ = await linked_token(ctx, ctx.customer)
    peer, _ = await linked_token(ctx, ctx.peer)
    path = f"/v1/console/servers/{server_id}"
    assert (await ctx.client.get(path, headers=bearer(peer))).status_code == 404
    assert (await ctx.client.get(path, headers=bearer(customer))).status_code == 200
    started = await ctx.client.post(
        path + "/actions/power-on", headers=bearer(customer, "customer-start-once"), json={}
    )
    assert started.status_code == 202 and started.json()["data"]["status"] == "running"
    assert ctx.provider.dispatches == 1
    ctx.provider.info = replace(ctx.provider.info, state="RUNNING")
    assert await lifecycle.reconcile_management(ctx.runtime) == 1
    result = await ctx.client.get(path + "/operations", headers=bearer(customer))
    assert any(
        row["action"] == "power-on" and row["status"] == "success" for row in result.json()["data"]
    )
    async with ctx.database() as session:
        assert (await session.get(Server, server_id)).user_id == ctx.customer


async def test_failed_confirmation_is_visible_private_and_never_redispatched(console_runtime):
    ctx = console_runtime
    admin, _ = await linked_token(ctx, ctx.owner)
    peer, _ = await linked_token(ctx, ctx.peer)
    path = f"/v1/console/servers/{ctx.server.id}"
    rejected = await ctx.client.post(
        path + "/actions/power-off",
        headers=bearer(admin, "rejected-stop"),
        json={"confirmationToken": "untrusted-confirmation"},
    )
    assert rejected.status_code == 409 and ctx.provider.dispatches == 0
    history = await ctx.client.get(path + "/operations", headers=bearer(admin))
    assert any(
        row["action"] == "power-off" and row["status"] == "failed" for row in history.json()["data"]
    )
    for action, body in (
        ("power-off", {"confirmationToken": "untrusted-confirmation"}),
        ("power-on", {}),
    ):
        replay = await ctx.client.post(
            path + "/actions/" + action,
            headers=bearer(admin, "rejected-stop"),
            json=body,
        )
        assert replay.status_code == 409
    own = await ctx.client.get("/v1/console/notifications", headers=bearer(admin))
    other = await ctx.client.get("/v1/console/notifications", headers=bearer(peer))
    assert len(own.json()["data"]) == 1 and other.json()["data"] == []
    async with ctx.database() as session:
        operations = (await session.execute(select(Operation))).scalars().all()
        assert len(operations) == 1 and operations[0].status == "failed"
        assert "untrusted-confirmation" not in str(operations[0].provider_response)
    deleted = await ctx.client.post(
        path + "/actions/delete", headers=bearer(admin, "protected-fixture"), json={}
    )
    assert deleted.status_code == 409 and ctx.provider.dispatches == 0
    missing_image = await ctx.client.post(
        path + "/actions/reinstall", headers=bearer(admin, "missing-image"), json={}
    )
    assert missing_image.status_code == 400
    absent = await ctx.client.get(f"/v1/console/servers/{uuid4()}", headers=bearer(admin))
    assert absent.status_code == 404
    async with ctx.database() as session:
        assert (await session.get(Server, ctx.server.id)).user_id == ctx.owner


async def test_privileged_user_search_hides_customer_data_after_role_revocation(console_runtime):
    ctx = console_runtime
    admin, _ = await linked_token(ctx, ctx.peer)
    customer, _ = await linked_token(ctx, ctx.customer)
    path = "/v1/console/admin/users"
    denied = await ctx.client.get(path, params={"q": "customer"}, headers=bearer(customer))
    assert denied.status_code == 403
    found = await ctx.client.get(path, params={"q": "customer"}, headers=bearer(admin))
    assert found.status_code == 200
    assert [row["id"] for row in found.json()["data"]] == [str(ctx.customer)]
    async with ctx.database.begin() as session:
        await session.execute(update(User).where(User.id == ctx.peer).values(role="user"))
    revoked = await ctx.client.get(path, params={"q": "customer"}, headers=bearer(admin))
    assert revoked.status_code == 403


async def test_missing_canonical_wallet_cannot_start_a_payable_invoice(console_runtime):
    from cloud_platform.db.base import LedgerEntry, PaymentSession, Wallet

    ctx = console_runtime
    customer, _ = await linked_token(ctx, ctx.customer)
    async with ctx.database.begin() as session:
        await session.execute(delete(Wallet).where(Wallet.user_id == ctx.customer))
    result = await ctx.client.post(
        "/v1/console/recharge",
        headers=bearer(customer, "missing-wallet"),
        json={"amountMinor": 2000, "gateway": "unconfigured"},
    )
    assert result.status_code == 404
    async with ctx.database() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentSession)) == 0
        assert await session.scalar(select(func.count()).select_from(LedgerEntry)) == 0
