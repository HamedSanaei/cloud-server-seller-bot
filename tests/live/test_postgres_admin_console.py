"""Opt-in isolated PostgreSQL contracts for real administrator mutation adapters."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from cloud_platform.db import base as db
from cloud_platform.modules.admin_console.messaging import (
    AdminMessagingWorker,
    MessageRecipient,
)
from cloud_platform.modules.admin_console.messaging_repository import (
    SqlAlchemyAdminMessagingRepository,
)
from cloud_platform.modules.admin_console.sales_repository import SqlAlchemyAdminSalesRepository
from cloud_platform.modules.admin_console.support import server_binding
from cloud_platform.modules.admin_console.support_repository import SqlAlchemyAdminSupportRepository
from cloud_platform.modules.compute.repository import SqlAlchemyServerRepository
from cloud_platform.modules.users.domain import PermissionDeniedError
from cloud_platform.modules.users.superadmin_repository import SqlAlchemySuperadminRepository
from tests.live.test_postgres_operator_cutover import DB_URL, TELEGRAM_ID, seed
from tests.live.test_postgres_operator_cutover import database as database

pytestmark = pytest.mark.skipif(not DB_URL, reason="explicit scratch loopback PostgreSQL required")


@pytest.fixture
async def prepared(database):
    manifest = await seed(database, count=2)
    customer, offer = uuid4(), uuid4()
    async with database.begin() as session:
        await session.execute(
            text(
                "CREATE TRIGGER admin_message_campaigns_immutable BEFORE UPDATE OR DELETE "
                "ON admin_message_campaigns FOR EACH ROW "
                "EXECUTE FUNCTION prevent_operator_financial_mutation()"
            )
        )
        session.add(
            db.User(
                id=customer,
                username=f"customer-{customer.hex}",
                email=f"{customer.hex}@example.test",
                telegram_user_id=30303,
                role="user",
                status="active",
            )
        )
        session.add(
            db.SellableOffer(
                id=offer,
                provider_key="fixture-provider",
                product_id="fixture",
                location_id="fixture-location",
                name="Fixture",
                provider_cost_minor=100,
                provider_cost_currency="USD",
                selling_price_minor=200,
                selling_currency="USD",
                enabled=True,
                provider_available=True,
            )
        )
    return SimpleNamespace(
        factory=database,
        actor=manifest.operator_id,
        customer=customer,
        offer=offer,
        server=manifest.entries[1].server_id,
    )


async def campaign(env, key="campaign-1"):
    repo = SqlAlchemyAdminMessagingRepository(env.factory, configured_ids=(TELEGRAM_ID,))
    identity = await repo.create_campaign(
        actor_id=env.actor,
        recipients=(MessageRecipient(env.customer, 30303),),
        body="Test message",
        reason="Test reason",
        idempotency_key=key,
        configured_ids=(TELEGRAM_ID,),
    )
    return repo, identity


async def test_approved_campaign_is_immutable_in_postgres(prepared):
    _, identity = await campaign(prepared)
    for statement in (
        update(db.AdminMessageCampaign)
        .where(db.AdminMessageCampaign.id == identity)
        .values(body="changed"),
        db.AdminMessageCampaign.__table__.delete().where(db.AdminMessageCampaign.id == identity),
    ):
        with pytest.raises(DBAPIError):
            async with prepared.factory.begin() as session:
                await session.execute(statement)


async def test_concurrent_provider_blocks_from_different_offers_share_one_scope(prepared):
    env = prepared
    other = uuid4()
    async with env.factory.begin() as session:
        session.add(
            db.SellableOffer(
                id=other,
                provider_key="fixture-provider",
                product_id="other",
                location_id="other-location",
                name="Other fixture",
                provider_cost_minor=100,
                provider_cost_currency="USD",
                selling_price_minor=200,
                selling_currency="USD",
                enabled=True,
                provider_available=True,
            )
        )
    repo = SqlAlchemyAdminSalesRepository(env.factory)

    async def apply(offer_id, location):
        return await repo.apply(
            actor_id=env.actor,
            configured_ids=(TELEGRAM_ID,),
            operation_key=f"block-{offer_id}",
            facts=dict(
                offer_id=str(offer_id),
                action="block_provider",
                reason="Planned maintenance",
                provider="fixture-provider",
                location=location,
            ),
        )

    assert (
        await asyncio.gather(apply(env.offer, "fixture-location"), apply(other, "other-location"))
        == ["block_provider"] * 2
    )
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.MaintenanceBlock)) == 1


async def test_campaign_atomic_replay_claim_fence_and_no_overlapping_deliveries(prepared):
    env = prepared
    repo, identity = await campaign(env)
    _, repeated = await campaign(env)
    assert identity == repeated
    first, second = await asyncio.gather(repo.claim_deliveries(), repo.claim_deliveries())
    assert len(first) + len(second) == 1
    delivery = (first or second)[0]
    assert await repo.may_deliver(delivery)
    assert not await repo.finish(replace(delivery, claim_token=uuid4()), "sent")
    assert await repo.finish(delivery, "sent")
    assert not await repo.may_deliver(delivery)
    assert not await repo.claim_deliveries()
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.AdminMessageCampaign)) == 1
        assert await session.scalar(select(func.count()).select_from(db.AdminMessageDelivery)) == 1


@pytest.mark.parametrize("change", ["actor_revoked", "recipient_rebound", "recipient_blocked"])
async def test_each_send_rechecks_fresh_actor_and_frozen_recipient(prepared, change):
    env = prepared
    repo, _ = await campaign(env)
    delivery = (await repo.claim_deliveries())[0]
    if change == "actor_revoked":
        await SqlAlchemySuperadminRepository(env.factory).reconcile([])
    else:
        async with env.factory.begin() as session:
            values = (
                {"telegram_user_id": 30304}
                if change == "recipient_rebound"
                else {"status": "banned"}
            )
            await session.execute(
                update(db.User).where(db.User.id == env.customer).values(**values)
            )
    assert not await repo.may_deliver(delivery)
    assert not await repo.claim_deliveries()


async def test_expired_lease_and_lost_acknowledgement_never_autoresend(prepared):
    env = prepared
    repo, _ = await campaign(env)
    delivery = (await repo.claim_deliveries())[0]
    async with env.factory.begin() as session:
        await session.execute(
            update(db.AdminMessageDelivery)
            .where(db.AdminMessageDelivery.id == delivery.id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert not await repo.claim_deliveries()
    assert not await repo.finish(delivery, "sent")
    await campaign(env, "campaign-2")
    sender = SimpleNamespace(send=fail_send)
    worker = AdminMessagingWorker(repo, sender)
    assert await worker.process_pending() == 1
    assert await worker.process_pending() == 0
    async with env.factory() as session:
        assert set((await session.scalars(select(db.AdminMessageDelivery.status))).all()) == {
            "ambiguous"
        }


async def fail_send(*_args):
    raise TimeoutError("test lost acknowledgement")


async def test_failed_campaign_audit_rolls_back_campaign_and_all_recipients(prepared):
    env = prepared
    async with env.factory.begin() as session:
        await session.execute(
            text("""CREATE FUNCTION reject_admin_audit() RETURNS trigger AS $$
          BEGIN RAISE EXCEPTION 'test audit failure' USING ERRCODE='23514'; END;
          $$ LANGUAGE plpgsql""")
        )
        await session.execute(
            text(
                "CREATE TRIGGER reject_audit BEFORE INSERT ON audit_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_admin_audit()"
            )
        )
    with pytest.raises(DBAPIError):
        await campaign(env)
    async with env.factory() as session:
        assert await session.scalar(select(func.count()).select_from(db.AdminMessageCampaign)) == 0
        assert await session.scalar(select(func.count()).select_from(db.AdminMessageDelivery)) == 0


async def test_sales_confirmation_replays_once_and_rejects_changed_facts_or_revocation(prepared):
    env = prepared
    repo = SqlAlchemyAdminSalesRepository(env.factory)
    facts = dict(
        offer_id=str(env.offer),
        action="set_price",
        reason="Reviewed retail price",
        provider="fixture-provider",
        location="fixture-location",
        price_minor=250,
        currency="USD",
        cost_minor=100,
        cost_currency="USD",
    )
    kwargs = dict(
        actor_id=env.actor, configured_ids=(TELEGRAM_ID,), operation_key="sales-1", facts=facts
    )
    assert await repo.apply(**kwargs) == "set_price"
    assert await repo.apply(**kwargs) == "set_price"
    with pytest.raises(ValueError, match="idempotency"):
        await repo.apply(**{**kwargs, "facts": {**facts, "price_minor": 251}})
    async with env.factory() as session:
        offer = await session.get(db.SellableOffer, env.offer)
        assert offer.selling_price_minor == 250 and offer.auto_priced is False
        assert (
            await session.scalar(
                select(func.count())
                .select_from(db.AuditEvent)
                .where(db.AuditEvent.action == "admin.sales.changed")
            )
            == 1
        )
    await SqlAlchemySuperadminRepository(env.factory).reconcile([])
    with pytest.raises(PermissionDeniedError):
        await repo.apply(**{**kwargs, "operation_key": "sales-2"})


async def test_support_has_immutable_actor_reason_and_dispatch_revocation_fence(prepared):
    env = prepared
    async with env.factory.begin() as session:
        await session.execute(
            update(db.Server)
            .where(db.Server.id == env.server)
            .values(state="running", provider_server_id="test-provider-resource")
        )
    server = await SqlAlchemyServerRepository(env.factory).get(env.server)
    repo = SqlAlchemyAdminSupportRepository(env.factory)
    kwargs = dict(
        actor_id=env.actor,
        server_id=env.server,
        action="power_off",
        operation_key="support-1",
        reason="Customer requested stop",
        expected_binding=server_binding(server),
        expected_state="running",
        configured_ids=(TELEGRAM_ID,),
    )
    operation = await repo.enqueue(**kwargs)
    assert (await repo.enqueue(**kwargs)).id == operation.id
    assert await repo.authorize_dispatch(operation, (TELEGRAM_ID,)) == env.actor
    with pytest.raises(ValueError, match="facts differ"):
        await repo.enqueue(**{**kwargs, "reason": "Different reason"})
    await SqlAlchemySuperadminRepository(env.factory).reconcile([])
    with pytest.raises(PermissionDeniedError):
        await repo.authorize_dispatch(operation, (TELEGRAM_ID,))
