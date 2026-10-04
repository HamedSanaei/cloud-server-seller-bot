"""Opt-in scratch-PostgreSQL contracts for the first customer sale.

Set CLOUD_PLATFORM_TEST_POSTGRES_URL to an isolated test database. These tests
never call a live payment gateway or provider, or send a Telegram message.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cloud_platform.db.base import AuditEvent, BusinessLogEvent, GatewaySetting, LedgerEntry
from cloud_platform.modules.businesslog.domain import BusinessLogPolicy, OutboxBusinessEventSink
from cloud_platform.modules.businesslog.repository import SqlAlchemyBusinessLogRepository
from cloud_platform.modules.payments.gateway_repository import SqlAlchemyGatewaySettingsRepository
from cloud_platform.modules.payments.gateways import GatewayManagementService
from cloud_platform.modules.payments.recharge import RechargeError, WalletRechargeService
from cloud_platform.modules.payments.repository import SqlAlchemyPaymentSessionRepository
from cloud_platform.modules.users.domain import Role, User
from cloud_platform.modules.users.identity import IdentityService, require_verified_identity
from cloud_platform.modules.users.repository import SqlAlchemyUserRepository
from cloud_platform.modules.wallet.domain import DuplicateIdempotencyError, LedgerEntryType
from cloud_platform.modules.wallet.repository import (
    SqlAlchemyLedgerRepository,
    SqlAlchemyWalletRepository,
)
from cloud_platform.modules.wallet.service import WalletAdminService
from cloud_platform.providers.atlaspay.client import AtlasPayGateway

DB_URL = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DB_URL, reason="requires an isolated scratch PostgreSQL database"
)


@pytest.fixture(scope="module", autouse=True)
def migrated() -> None:
    if not DB_URL:
        return
    env = dict(os.environ, DATABASE_URL=DB_URL)
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
async def factory():
    engine = create_async_engine(DB_URL, hide_parameters=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def make_user(factory, *, admin=False, currency="USD"):
    suffix = uuid4().hex
    user = await SqlAlchemyUserRepository(factory).create(
        User(
            id=uuid4(),
            username="verify-" + suffix,
            email=suffix + "@example.test",
            telegram_user_id=int(suffix[:14], 16),
            role=Role.ADMIN if admin else Role.USER,
        )
    )
    await SqlAlchemyWalletRepository(factory).get_or_create(user.id, currency=currency)
    return user


async def test_identity_reload_preserves_aware_verification_instant(factory):
    user = await make_user(factory)
    repository = SqlAlchemyUserRepository(factory)
    service = IdentityService(repository)
    verified_at = datetime(2026, 10, 4, 12, 30, tzinfo=timezone(timedelta(hours=3, minutes=30)))
    user = await service.verify_contact(
        user,
        actor_telegram_user_id=user.telegram_user_id,
        contact_user_id=user.telegram_user_id,
        phone_number="0098 912 345 6789",
        at=verified_at,
    )
    await service.collect_national_id(
        user,
        actor_telegram_user_id=user.telegram_user_id,
        national_id="۱۲۳۴۵۶۷۸۹۱",
    )
    reloaded = await repository.get(user.id)
    assert reloaded.phone_number == "+989123456789"
    assert reloaded.phone_verified_at == verified_at.astimezone(UTC)
    assert reloaded.national_id == "1234567891"
    require_verified_identity(reloaded)


async def test_gateway_switch_persists_actor_and_aware_timestamp(factory):
    admin = await make_user(factory, admin=True)
    key = "verify-" + uuid4().hex[:16]
    repository = SqlAlchemyGatewaySettingsRepository(factory)
    management = GatewayManagementService(repository, [key])
    assert (await management.list(admin))[0].enabled is True
    await management.set_enabled(admin, key, False)
    assert await repository.get(key) is False
    async with factory() as session:
        row = await session.get(GatewaySetting, key)
        assert row.updated_by == admin.id
        assert row.updated_at.tzinfo is not None
    await management.set_enabled(admin, key, True)
    assert (await GatewayManagementService(repository, [key]).list(admin))[0].enabled is True


async def test_manual_credit_commits_wallet_ledger_audit_and_outbox_once(factory):
    admin = await make_user(factory, admin=True)
    user = await make_user(factory)
    wallet_repository = SqlAlchemyWalletRepository(factory)
    ledger_repository = SqlAlchemyLedgerRepository(factory)
    policy = BusinessLogPolicy(enabled=True, chat_id=123)
    sink = OutboxBusinessEventSink(SqlAlchemyBusinessLogRepository(factory), policy)
    service = WalletAdminService(
        wallet_repository,
        ledger_repository,
        event_sink=sink,
        user_repo=SqlAlchemyUserRepository(factory),
    )
    key = "verify-credit:" + uuid4().hex
    outcomes = await asyncio.gather(
        *[
            service.adjust_balance(
                admin=admin,
                user_id=user.id,
                amount=1050,
                reason="first sale credit",
                idempotency_key=key,
            )
            for _ in range(2)
        ]
    )
    wallet = await wallet_repository.get(user.id)
    assert wallet.balance == 1050
    assert outcomes[0][1].id == outcomes[1][1].id
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(LedgerEntry.wallet_id == wallet.id)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.resource_id == str(wallet.id))
            )
            == 1
        )
        events = (
            (
                await session.execute(
                    select(BusinessLogEvent).where(
                        BusinessLogEvent.payload["admin_id"].astext == str(admin.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].payload["balance_after"] == "$10.50"
    with pytest.raises(DuplicateIdempotencyError):
        await service.adjust_balance(
            admin=admin,
            user_id=user.id,
            amount=1051,
            reason="first sale credit",
            idempotency_key=key,
        )
    with pytest.raises(DuplicateIdempotencyError):
        await service.adjust_balance(
            admin=admin,
            user_id=user.id,
            amount=-1050,
            reason="first sale credit",
            idempotency_key=key,
        )
    assert (await wallet_repository.get(user.id)).balance == 1050


async def test_outbox_insert_failure_rolls_back_manual_credit_and_audit(factory):
    from cloud_platform.modules.businesslog.events import admin_adjustment_event

    admin = await make_user(factory, admin=True)
    user = await make_user(factory)
    repository = SqlAlchemyWalletRepository(factory)
    key = "verify-rollback:" + uuid4().hex
    event = admin_adjustment_event(
        admin=admin,
        user=user,
        amount_minor=500,
        currency="USD",
        reason="rollback",
        balance_after_minor=500,
        entry_type="adjustment",
        idempotency_key=key,
    )
    await SqlAlchemyBusinessLogRepository(factory).enqueue(
        event_key=event.event_key,
        event_type=event.event_type.value,
        payload=event.sanitized_payload(),
    )
    with pytest.raises(IntegrityError):
        await repository.adjust(
            user.id,
            500,
            key,
            entry_type=LedgerEntryType.ADJUSTMENT,
            reference_type="admin_adjustment",
            reference_id=str(admin.id),
            description="rollback",
            audit_actor_id=admin.id,
            business_event_factory=lambda wallet: event,
        )
    wallet = await repository.get(user.id)
    assert wallet.balance == 0
    assert (
        await SqlAlchemyLedgerRepository(factory).get_entry_by_idempotency(wallet.id, key) is None
    )
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.resource_id == str(wallet.id))
            )
            == 0
        )


async def test_concurrent_atlas_attempt_does_not_repeat_nonidempotent_post(factory):
    user = await make_user(factory, currency="IRT")
    entered, release = asyncio.Event(), asyncio.Event()
    posts = []
    order_id = int(uuid4().hex[:7], 16) + 1

    async def respond(request):
        posts.append(json.loads(request.content))
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "orderId": order_id,
                "trackingCode": "verifytrack123",
                "totalAmountToman": 250019,
                "cardNumberMasked": "6037****3165",
                "paymentDeadlineAt": "2026-10-08T12:20:00Z",
                "customerStartLink": "https://t.me/atlaspaybot/pay?startapp=verify",
            },
        )

    gateway = AtlasPayGateway("fixture-only-key", transport=httpx.MockTransport(respond))
    service = WalletRechargeService(
        payments_repo=SqlAlchemyPaymentSessionRepository(factory),
        gateway=gateway,
    )
    key = "verify-atlas:" + uuid4().hex
    first = asyncio.create_task(
        service.start(user=user, amount_minor=250000, currency="IRT", idempotency_key=key)
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        with pytest.raises(RechargeError, match="unknown"):
            await service.start(user=user, amount_minor=250000, currency="IRT", idempotency_key=key)
        release.set()
        result = await first
        assert result.session.amount_minor == 250019
        assert result.session.effective_credit_amount == 250000
        assert len(posts) == 1
    finally:
        release.set()
        if not first.done():
            await first
        await gateway.close()


async def test_manual_credit_normalizes_key_before_atomic_replay(factory):
    admin = await make_user(factory, admin=True)
    user = await make_user(factory)
    service = WalletAdminService(
        SqlAlchemyWalletRepository(factory), SqlAlchemyLedgerRepository(factory)
    )
    key = "verify-spaces:" + uuid4().hex
    first = await service.adjust_balance(
        admin=admin, user_id=user.id, amount=100, reason="credit", idempotency_key=" " + key + " "
    )
    second = await service.adjust_balance(
        admin=admin, user_id=user.id, amount=100, reason="credit", idempotency_key=key
    )
    assert first[1].id == second[1].id
    assert second[0].balance == 100


async def test_admin_confirmation_restart_replay_and_cancel_isolation(factory):
    from cloud_platform.bot.admin_ui import AdminBotUi
    from cloud_platform.core.session_store import InMemoryBotSessionStore
    from cloud_platform.modules.navigation.domain import Callback, decode_callback

    admin = await make_user(factory, admin=True)
    user = await make_user(factory)
    users = SqlAlchemyUserRepository(factory)
    wallets = SqlAlchemyWalletRepository(factory)
    credit = WalletAdminService(wallets, SqlAlchemyLedgerRepository(factory))
    store = InMemoryBotSessionStore()
    signing_key = "fixture-signing-key"

    def ui():
        return AdminBotUi(
            signing_key,
            gateways=GatewayManagementService(
                SqlAlchemyGatewaySettingsRepository(factory), ["atlaspay"]
            ),
            users=users,
            wallets=wallets,
            wallet_admin=credit,
            store=store,
            ttl_seconds=600,
        )

    await ui().handle(Callback("admin", "credit"), admin)
    await ui().handle_text(str(user.telegram_user_id), admin)
    await ui().handle_text("10.000000000000000000000000000001", admin)
    assert (await store.get("admin-credit", str(admin.id)))["step"] == "amount"
    await ui().handle_text("۱۰.۵۰", admin)  # noqa: RUF001 -- Iranian decimal input
    confirmation = await ui().handle_text("first customer", admin)
    buttons = [b for row in confirmation.keyboard.inline_keyboard for b in row]
    apply, cancel = [decode_callback(b.callback_data, signing_key) for b in buttons]
    assert (await wallets.get(user.id)).balance == 0
    # A canceled older confirmation must not destroy a newer credit prompt.
    await ui().handle(Callback("admin", "credit"), admin)
    await ui().handle(cancel, admin)
    assert (await store.get("admin-credit", str(admin.id)))["step"] == "target"
    await ui().handle(apply, admin)
    assert (await wallets.get(user.id)).balance == 0
    await ui().handle_text(str(user.telegram_user_id), admin)
    await ui().handle_text("10.50", admin)
    confirmation = await ui().handle_text("first customer", admin)
    apply = decode_callback(confirmation.keyboard.inline_keyboard[0][0].callback_data, signing_key)
    await ui().handle(apply, admin)
    await ui().handle(apply, admin)
    assert (await wallets.get(user.id)).balance == 1050


async def test_identity_flow_survives_restart_and_rejects_foreign_contact(factory, monkeypatch):
    from aiogram.types import Chat, Contact, Message
    from aiogram.types import User as TelegramUser

    from cloud_platform.bot.identity_flow import IdentityFlow
    from cloud_platform.core.session_store import InMemoryBotSessionStore

    user = await make_user(factory)
    repository = SqlAlchemyUserRepository(factory)
    store = InMemoryBotSessionStore()
    sent = []

    async def answer(message, text, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(Message, "answer", answer)

    def message(*, text=None, contact_id=None):
        return Message(
            message_id=1,
            date=1,
            chat=Chat(id=user.telegram_user_id, type="private"),
            from_user=TelegramUser(id=user.telegram_user_id, is_bot=False, first_name="Fixture"),
            text=text,
            contact=Contact(phone_number="09123456789", first_name="Fixture", user_id=contact_id)
            if contact_id is not None
            else None,
        )

    def flow():
        return IdentityFlow(IdentityService(repository), store, ttl_seconds=600)

    assert await flow().begin(message(), user, "signed-purchase-continuation") is True
    assert sent[-1][1].keyboard[0][0].request_contact is True
    await flow().handle(message(contact_id=user.telegram_user_id + 1), user)
    assert (await repository.get(user.id)).phone_verified_at is None
    await flow().handle(message(contact_id=user.telegram_user_id), user)
    assert (await store.get("identity", str(user.id)))["step"] == "national"
    user = await repository.get(user.id)
    handled, continuation = await flow().handle(message(text="۱۲۳۴۵۶۷۸۹۱"), user)
    assert handled is True
    assert continuation == "signed-purchase-continuation"
    require_verified_identity(await repository.get(user.id))
    assert await store.get("identity", str(user.id)) is None
    verified = await repository.get(user.id)
    assert await flow().begin(message(), verified, "another-purchase") is False
    assert await flow().handle(message(text="1234567891"), verified) == (False, None)


@pytest.mark.parametrize("case", ["group", "other_private_chat", "anonymous"])
async def test_identity_prompt_never_binds_another_chat_or_anonymous_user(
    factory,
    monkeypatch,
    case,
):
    from aiogram.types import Chat, Message

    from cloud_platform.bot.identity_flow import IdentityFlow
    from cloud_platform.core.session_store import InMemoryBotSessionStore

    user = await make_user(factory)
    stored_id = user.id
    repository = SqlAlchemyUserRepository(factory)
    store = InMemoryBotSessionStore()
    sent = []

    async def answer(message, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    chat_id = user.telegram_user_id if case == "anonymous" else user.telegram_user_id + 1
    message = Message(
        message_id=1,
        date=1,
        chat=Chat(id=chat_id, type="group" if case == "group" else "private"),
    )
    if case == "anonymous":
        user.id = None
    flow = IdentityFlow(IdentityService(repository), store, ttl_seconds=600)
    assert await flow.begin(message, user, "purchase") is (case != "anonymous")
    assert await flow.handle(message, user) == (False, None)
    assert await store.get("identity", str(stored_id)) is None
    persisted = await repository.get(stored_id)
    assert persisted.phone_verified_at is None
    assert persisted.national_id is None


async def test_identity_cancel_invalidates_continuation_without_losing_verified_phone(
    factory,
    monkeypatch,
):
    from aiogram.types import Chat, Message
    from aiogram.types import User as TelegramUser

    from cloud_platform.bot.identity_flow import IdentityFlow
    from cloud_platform.bot.identity_ui import national_id_screen
    from cloud_platform.core.session_store import InMemoryBotSessionStore

    user = await make_user(factory)
    repository = SqlAlchemyUserRepository(factory)
    user = await IdentityService(repository).verify_contact(
        user,
        actor_telegram_user_id=user.telegram_user_id,
        contact_user_id=user.telegram_user_id,
        phone_number="09123456789",
    )
    store = InMemoryBotSessionStore()

    async def answer(message, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", answer)
    message = Message(
        message_id=1,
        date=1,
        chat=Chat(id=user.telegram_user_id, type="private"),
        from_user=TelegramUser(id=user.telegram_user_id, is_bot=False, first_name="Fixture"),
        text=national_id_screen().keyboard.keyboard[0][0].text,
    )
    flow = IdentityFlow(IdentityService(repository), store, ttl_seconds=600)
    assert await flow.begin(message, user, "signed-purchase") is True
    assert await store.get("identity", str(user.id)) == {
        "step": "national",
        "resume": "signed-purchase",
    }
    assert await flow.handle(message, user) == (True, None)
    assert await store.get("identity", str(user.id)) is None
    assert await flow.handle(message, user) == (False, None)
    persisted = await repository.get(user.id)
    assert persisted.phone_number == "+989123456789"
    assert persisted.phone_verified_at == user.phone_verified_at
    assert persisted.national_id is None


@pytest.mark.parametrize("case", ["corrupt_stage", "storage_failure"])
async def test_identity_corruption_or_storage_failure_never_resumes_or_leaks_contact(
    factory,
    monkeypatch,
    case,
):
    from unittest.mock import AsyncMock

    from aiogram.types import Chat, Contact, Message
    from aiogram.types import User as TelegramUser
    from sqlalchemy.exc import SQLAlchemyError

    from cloud_platform.bot.identity_flow import IdentityFlow
    from cloud_platform.core.session_store import InMemoryBotSessionStore

    user = await make_user(factory)
    repository = SqlAlchemyUserRepository(factory)
    store = InMemoryBotSessionStore()
    sent = []

    async def answer(message, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer)
    message = Message(
        message_id=1,
        date=1,
        chat=Chat(id=user.telegram_user_id, type="private"),
        from_user=TelegramUser(id=user.telegram_user_id, is_bot=False, first_name="Fixture"),
        contact=Contact(
            phone_number="09123456789",
            first_name="Fixture",
            user_id=user.telegram_user_id,
        ),
    )
    flow = IdentityFlow(IdentityService(repository), store, ttl_seconds=600)
    await flow.begin(message, user, "signed-purchase")
    if case == "corrupt_stage":
        await store.put(
            "identity",
            str(user.id),
            {"step": "unknown", "resume": "signed-purchase"},
            ttl_seconds=600,
        )
    else:
        monkeypatch.setattr(
            repository,
            "update_verified_phone",
            AsyncMock(side_effect=SQLAlchemyError("09123456789 private driver parameters")),
        )
    before = await store.get("identity", str(user.id))
    assert await flow.handle(message, user) == (True, None)
    assert await store.get("identity", str(user.id)) == before
    persisted = await repository.get(user.id)
    assert persisted.phone_number is None
    assert persisted.phone_verified_at is None
    assert persisted.national_id is None
    assert "09123456789" not in sent[-1]
    assert "private driver parameters" not in sent[-1]


async def seed_hourly_contract(factory):
    from decimal import Decimal
    from types import SimpleNamespace

    from cloud_platform.db.base import Provider, ProviderAccount, Server
    from cloud_platform.modules.audit.repository import SqlAlchemyAuditRepository
    from cloud_platform.modules.billing.repository import (
        PostgresAdvisoryAccrualLock,
        SqlAlchemyAccrualPeriodRepository,
    )
    from cloud_platform.modules.billing.service import AccrualJob
    from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
    from cloud_platform.modules.pricing.domain import MarginRule, OfferCost, ServerPriceSnapshot
    from cloud_platform.modules.pricing.repository import SqlAlchemyServerPriceSnapshotRepository
    from cloud_platform.modules.wallet.repository import HoldService, SqlAlchemyHoldRepository

    admin = await make_user(factory, admin=True)
    user = await make_user(factory)
    wallets, ledger = SqlAlchemyWalletRepository(factory), SqlAlchemyLedgerRepository(factory)
    suffix = uuid4().hex
    await WalletAdminService(wallets, ledger).adjust_balance(
        admin=admin,
        user_id=user.id,
        amount=300,
        reason="fund hourly contract",
        idempotency_key="verify-hourly-credit:" + suffix,
    )
    provider_id, account_id, server_id = uuid4(), uuid4(), uuid4()
    provider_key = "verify-" + suffix
    creation_key = "verify-hourly-create:" + suffix
    async with factory() as session:
        session.add(Provider(id=provider_id, name=provider_key))
        await session.flush()
        session.add(ProviderAccount(id=account_id, provider_id=provider_id, user_id=user.id))
        await session.flush()
        session.add(
            Server(
                id=server_id,
                user_id=user.id,
                provider_id=provider_id,
                provider_account_id=account_id,
                state="provisioning",
                provider_server_id="fixture-only-resource",
                price_per_quantum=80,
                currency="USD",
                quantum_seconds=3600,
                idempotency_key=creation_key,
            )
        )
        await session.commit()
    snapshots = SqlAlchemyServerPriceSnapshotRepository(factory)
    activation = datetime(2026, 10, 4, 12, 0, 30, 123456, tzinfo=UTC)
    await snapshots.create(
        ServerPriceSnapshot(
            server_id=server_id,
            offer=OfferCost(
                provider_key=provider_key,
                plan_id="fixture",
                location_id="fixture",
                cost_minor=80,
                currency="USD",
                provider_rate_exact="0.80",
            ),
            selling_minor=100,
            book_name="verify",
            book_version=1,
            rule=MarginRule(provider="*", plan="*", location="*", margin_factor=Decimal("1.25")),
            priced_at=activation,
        )
    )
    servers = SqlAlchemyServerRepository(factory)
    holds = SqlAlchemyHoldRepository(factory)
    hold_service = HoldService(wallets, holds, ledger)
    wallet = await wallets.get(user.id)
    await hold_service.create_hold(wallet.id, 100, "USD", "server-create:" + creation_key)
    job = AccrualJob(
        server_repo=servers,
        wallet_repo=wallets,
        hold_repo=holds,
        hold_service=hold_service,
        ledger_repo=ledger,
        accrual_repo=SqlAlchemyAccrualPeriodRepository(factory),
        snapshot_repo=snapshots,
        audit_repo=SqlAlchemyAuditRepository(factory),
        lock=PostgresAdvisoryAccrualLock(factory),
    )
    return SimpleNamespace(
        job=job,
        servers=servers,
        wallets=wallets,
        wallet=wallet,
        user=user,
        admin=admin,
        server_id=server_id,
        activation=activation,
    )


async def test_postgres_prepaid_capture_stale_anchor_and_early_renewal(factory):
    from cloud_platform.modules.billing.service import BillingLockBusyError

    contract = await seed_hourly_contract(factory)
    job, servers, wallets = contract.job, contract.servers, contract.wallets
    server_id, activation, user, wallet = (
        contract.server_id,
        contract.activation,
        contract.user,
        contract.wallet,
    )
    first, stale = await servers.get(server_id), await servers.get(server_id)
    results = await asyncio.gather(
        job.prepay_server(first, activation),
        job.prepay_server(stale, activation + timedelta(seconds=2)),
        return_exceptions=True,
    )
    for supplied, moment, outcome in zip(
        [first, stale],
        [activation, activation + timedelta(seconds=2)],
        results,
        strict=True,
    ):
        if isinstance(outcome, BillingLockBusyError):
            await job.prepay_server(supplied, moment)
        elif isinstance(outcome, BaseException):
            raise outcome
    persisted = await servers.get(server_id)
    anchor = persisted.billing_started_at
    assert anchor in {activation, activation + timedelta(seconds=2)}
    assert persisted.last_accrued_at == anchor + timedelta(hours=1)
    assert (await wallets.get(user.id)).balance == 200
    await job.prepay_server(first, anchor + timedelta(minutes=10))
    assert first.billing_started_at == anchor
    boundary = persisted.last_accrued_at
    await job.prepay_server(
        first,
        boundary - timedelta(seconds=5),
        renew_ahead_seconds=5,
    )
    assert first.last_accrued_at == boundary + timedelta(hours=1)
    assert (await wallets.get(user.id)).balance == 100
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(
                    LedgerEntry.wallet_id == wallet.id,
                    LedgerEntry.entry_type == "charge",
                )
            )
            == 2
        )


async def test_application_gateway_disable_keeps_exact_pending_credit(factory, monkeypatch):
    import cloud_platform.core.container as container_module
    import cloud_platform.providers.atlaspay.client as atlas_module
    from cloud_platform.core.config import Settings

    settings = Settings(
        _env_file=None,
        hetzner_api_token="",
        hetzner_accounts=[],
        leaseweb_api_key="",
        leaseweb_accounts=[],
        arvancloud_api_key="",
        database_url=DB_URL,
        fx_enabled=False,
        zarinpal_enabled=False,
        tetraminator_enabled=False,
        atlaspay_enabled=True,
        atlaspay_api_key="testkey",  # pragma: allowlist secret -- offline fixture
        telegram_sessions_backend="memory",
    )
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    merchant_refs = []
    order_id = int(uuid4().hex[:15], 16) + 1

    def transport(request):
        if request.url.path.endswith("/verify"):
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "id": order_id,
                    "trackingCode": "a1a1a1a1a1a1a1a1",
                    "merchantOrderRef": merchant_refs[0],
                    "status": "confirmed",
                    "totalAmountToman": 259739,
                    "actualReceivedAmountToman": 259739,
                    "requiresManualDelivery": False,
                    "paid": True,
                },
            )
        merchant_refs.append(json.loads(request.content)["merchantOrderRef"])
        return httpx.Response(
            200,
            json={
                "orderId": order_id,
                "trackingCode": "a1a1a1a1a1a1a1a1",
                "totalAmountToman": 259739,
                "customerStartLink": "https://t.me/atlaspaybot/pay?startapp=fixture-only",
                "paymentDeadlineAt": "2026-10-04T23:59:59Z",
            },
        )

    class OfflineAtlas(AtlasPayGateway):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(atlas_module, "AtlasPayGateway", OfflineAtlas)
    container = container_module.create_container()
    gateways = container.payment_gateways()
    async with factory() as session:
        original_switch = await session.get(GatewaySetting, "atlaspay")
        if original_switch is not None:
            session.expunge(original_switch)
    try:
        admin = await make_user(factory, admin=True)
        user = await make_user(factory, currency="IRT")
        management = container.gateway_management_service(gateways)
        await management.set_enabled(admin=admin, key="atlaspay", enabled=True)
        recharge = container.wallet_recharge_service(gateways=gateways)
        started = await recharge.start(
            user=user,
            amount_minor=250000,
            currency="IRT",
            idempotency_key="app-pay:" + uuid4().hex,
        )
        assert started.session.amount_minor == 259739
        await management.set_enabled(admin=admin, key="atlaspay", enabled=False)
        with pytest.raises(RechargeError):
            await recharge.start(
                user=user,
                amount_minor=250000,
                currency="IRT",
                idempotency_key="app-disabled:" + uuid4().hex,
            )
        inquiry = container.payment_inquiry_service(gateways)
        await inquiry.check_status(user, started.session.id)
        await inquiry.check_status(user, started.session.id)
        assert (await container.wallet_repository().get(user.id)).balance == 250000
        assert len(merchant_refs) == 1
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(LedgerEntry)
                    .where(
                        LedgerEntry.wallet_id
                        == (await container.wallet_repository().get(user.id)).id,
                        LedgerEntry.entry_type == "deposit",
                    )
                )
                == 1
            )
    finally:
        async with factory() as session:
            if original_switch is not None:
                await session.merge(original_switch)
            else:
                temporary_switch = await session.get(GatewaySetting, "atlaspay")
                if temporary_switch is not None:
                    await session.delete(temporary_switch)
            await session.commit()
        await container.aclose_gateways(gateways)
        await container.close()


async def test_real_worker_preserves_paid_hour_then_requests_expiry_delete(factory, monkeypatch):
    from unittest.mock import AsyncMock

    from arq import Retry

    import cloud_platform.core.config as config_module
    import cloud_platform.core.container as container_module
    import cloud_platform.db.session as db_session
    import cloud_platform.modules.billing.service as billing_module
    import cloud_platform.providers.registry as registry_module
    import cloud_platform.worker.settings as worker
    from cloud_platform.core.config import Settings
    from cloud_platform.modules.compute.domain import ServerLifecycleState
    from cloud_platform.providers.base import Capability, ProviderServer
    from cloud_platform.providers.errors import ProviderNotFound
    from cloud_platform.providers.registry import ProviderRegistry

    contract = await seed_hourly_contract(factory)
    server = await contract.servers.get(contract.server_id)
    await contract.job.prepay_server(server, contract.activation)
    server.transition_to(ServerLifecycleState.RUNNING)
    await contract.servers.save(server)
    boundary = server.last_accrued_at
    admin_service = WalletAdminService(contract.wallets, SqlAlchemyLedgerRepository(factory))
    await admin_service.adjust_balance(
        admin=contract.admin,
        user_id=contract.user.id,
        amount=-200,
        reason="exhaust remaining wallet",
        idempotency_key="app-exhaust:" + uuid4().hex,
    )
    moment = boundary - timedelta(seconds=5)
    settings = Settings(
        _env_file=None,
        hetzner_api_token="",
        hetzner_accounts=[],
        leaseweb_api_key="",
        leaseweb_accounts=[],
        arvancloud_api_key="",
        database_url=DB_URL,
        fx_enabled=False,
        zarinpal_enabled=False,
        tetraminator_enabled=False,
        atlaspay_enabled=False,
        telegram_sessions_backend="memory",
    )
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(db_session, "SessionFactory", factory)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(billing_module, "datetime", Clock)

    # A normalized in-memory provider port exercises the real deletion saga
    # without touching a live provider or replacing its financial behavior.
    class LocalCloud:
        key = server.provider_key
        capabilities = frozenset({Capability.COMPUTE})
        exists = True

        async def get_server(self, provider_server_id):
            if provider_server_id != server.provider_server_id or not self.exists:
                return None
            return ProviderServer(id=provider_server_id, name="fixture", status="running")

        async def delete_server(self, provider_server_id, idempotency_key):
            if provider_server_id != server.provider_server_id:
                raise ProviderNotFound("not the fixture resource")
            self.exists = False

    local_cloud = LocalCloud()
    registry = ProviderRegistry()
    registry.register(local_cloud)
    monkeypatch.setattr(registry_module, "ProviderRegistry", lambda: registry)
    redis = AsyncMock()
    with pytest.raises(Retry):
        await worker.renew_hourly_coverage({"redis": redis}, str(server.id))
    persisted = await contract.servers.get(server.id)
    assert persisted.state is ServerLifecycleState.RUNNING
    assert persisted.last_accrued_at == boundary
    assert (await contract.wallets.get(contract.user.id)).balance == 0
    await admin_service.adjust_balance(
        admin=contract.admin,
        user_id=contract.user.id,
        amount=100,
        reason="buy next hour",
        idempotency_key="app-refill:" + uuid4().hex,
    )
    await worker.renew_hourly_coverage({"redis": redis}, str(server.id))
    persisted = await contract.servers.get(server.id)
    assert persisted.last_accrued_at == boundary + timedelta(hours=1)
    assert (await contract.wallets.get(contract.user.id)).balance == 0
    moment = persisted.last_accrued_at
    await worker.renew_hourly_coverage({"redis": redis}, str(server.id))
    persisted = await contract.servers.get(server.id)
    assert persisted.state is ServerLifecycleState.DELETED
    assert local_cloud.exists is False
    assert persisted.last_accrued_at == moment
    assert (await contract.wallets.get(contract.user.id)).balance == 0
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(LedgerEntry)
                .where(
                    LedgerEntry.wallet_id == contract.wallet.id,
                    LedgerEntry.entry_type == "charge",
                )
            )
            == 2
        )


async def test_verified_application_checkout_reserves_one_frozen_hour_without_post(
    factory, monkeypatch
):
    import base64
    from dataclasses import replace

    from test_postgres_catalog_sync import _official_types_payload, _StubCloudTransport

    import cloud_platform.core.container as container_module
    from cloud_platform.core.config import Settings
    from cloud_platform.db.base import Provider
    from cloud_platform.modules.compute.domain import ServerLifecycleState
    from cloud_platform.modules.offers.domain import BILLING_MODEL_HOURLY
    from cloud_platform.modules.pricing.repository import SqlAlchemyServerPriceSnapshotRepository
    from cloud_platform.modules.users.identity import IdentityRequiredError
    from cloud_platform.modules.wallet.repository import SqlAlchemyHoldRepository
    from cloud_platform.providers.leaseweb.cloud import LeasewebHourlyCloudProvider
    from cloud_platform.providers.leaseweb.cloud_sync import offer_spec_from_item

    suffix = uuid4().hex
    product_id = "lsw.verify." + suffix

    class ReadOnlyTransport(_StubCloudTransport):
        async def request(self, method, path, **kwargs):
            assert method == "GET", "checkout must not issue a billable provider mutation"
            if path == "/publicCloud/v1/instanceTypes":
                payload = _official_types_payload()
                payload["_metadata"] = {"currency": "USD", "currencySymbol": "$"}
                payload["instanceTypes"][0]["name"] = product_id
                return payload
            return await super().request(method, path, **kwargs)

    adapter = LeasewebHourlyCloudProvider.__new__(LeasewebHourlyCloudProvider)
    adapter._transport = ReadOnlyTransport()
    settings = Settings(
        _env_file=None,
        database_url=DB_URL,
        fx_enabled=False,
        hetzner_api_token="",
        hetzner_accounts=[],
        leaseweb_api_key="",
        leaseweb_accounts=[],
        arvancloud_api_key="",
        zarinpal_enabled=False,
        tetraminator_enabled=False,
        atlaspay_enabled=False,
        telegram_sessions_backend="memory",
        provider_credential_encryption_key=base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
    )
    monkeypatch.setattr(container_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        container_module.Container,
        "hourly_cloud_providers",
        lambda self: {"leaseweb": adapter},
    )
    container = container_module.create_container()
    try:
        async with factory() as session:
            if await session.scalar(select(Provider).where(Provider.name == "leaseweb")) is None:
                session.add(Provider(id=uuid4(), name="leaseweb"))
                await session.commit()
        admin = await make_user(factory, admin=True)
        user = await make_user(factory)
        wallets = container.wallet_repository()
        await WalletAdminService(wallets, container.ledger_repository()).adjust_balance(
            admin=admin,
            user_id=user.id,
            amount=100,
            reason="first verified checkout",
            idempotency_key="app-hourly-fund:" + suffix,
        )
        instance_type = (await adapter.list_instance_types("eu-west-3"))[0]
        update = replace(
            offer_spec_from_item(
                instance_type,
                "eu-west-3",
                publishable=True,
                account_id="default",
            ),
            provider_account_id=None,
        )
        offers = container.sellable_offer_repository()
        offer = await offers.upsert_from_provider(
            provider_key="leaseweb",
            product_id=product_id,
            location_id="eu-west-3",
            update=update,
        )
        assert offer.billing_model == BILLING_MODEL_HOURLY
        management = container.offer_admin_service()
        await management.set_selling_price(
            actor=admin,
            offer_id=offer.id,
            selling_price_minor=6,
            currency="USD",
            reason="explicit first-hour price",
        )
        await offers.set_enabled(offer.id, True)
        service = container.hourly_cloud_service()
        request = {
            "offer_id": offer.id,
            "image_id": "ubuntu-24.04",
            "image_label": "Ubuntu 24.04",
            "idempotency_key": "app-hourly-create:" + suffix,
            "expected_selling_price_minor": 6,
            "expected_selling_currency": "USD",
        }
        with pytest.raises(IdentityRequiredError):
            await service.create_instance(user=user, **request)
        holds = SqlAlchemyHoldRepository(factory)
        wallet = await wallets.get(user.id)
        assert await holds.active_hold_sum(wallet.id) == 0
        identity = IdentityService(container.user_repository())
        user = await identity.verify_contact(
            user,
            actor_telegram_user_id=user.telegram_user_id,
            contact_user_id=user.telegram_user_id,
            phone_number="+989123456789",
        )
        user = await identity.collect_national_id(
            user,
            actor_telegram_user_id=user.telegram_user_id,
            national_id="1234567891",
        )
        created = await service.create_instance(user=user, **request)
        assert created.server.state is ServerLifecycleState.REQUESTED
        assert created.server.provider_server_id is None
        assert (await wallets.get(user.id)).balance == 100
        assert await holds.active_hold_sum(wallet.id) == 6
        snapshot_repo = SqlAlchemyServerPriceSnapshotRepository(factory)
        snapshot = await snapshot_repo.get(created.server.id)
        assert snapshot.selling_minor == 6 and snapshot.selling_currency == "USD"
        assert snapshot.offer.provider_rate_exact == "0.0395"
        await management.set_selling_price(
            actor=admin,
            offer_id=offer.id,
            selling_price_minor=9,
            currency="USD",
            reason="reprice catalog, not accepted contract",
        )
        replay = await service.create_instance(user=user, **request)
        assert replay.server.id == created.server.id
        assert await holds.active_hold_sum(wallet.id) == 6
        assert (await snapshot_repo.get(created.server.id)).selling_minor == 6
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(LedgerEntry)
                    .where(
                        LedgerEntry.wallet_id == wallet.id,
                        LedgerEntry.entry_type == "charge",
                    )
                )
                == 0
            )
    finally:
        await adapter.close()
        await container.close()
