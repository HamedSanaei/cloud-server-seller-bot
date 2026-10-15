"""Production IRT reservations survive account failover and uncertain creates."""

from copy import deepcopy

import pytest

from cloud_platform.modules.compute.domain import BILLING_MODEL_PREPAID_HOURLY_IRT
from cloud_platform.modules.wallet.domain import HoldStatus
from tests.unit.test_hetzner_hourly_failover import Pool, ReceiptPort
from tests.unit.test_hourly_cloud_flow import FakeOffersRepo
from tests.unit.test_prepaid_hourly_checkout import checkout


@pytest.mark.parametrize("failure", ["capacity", "unknown"])
async def test_one_irt_quote_and_hold_for_the_same_multi_account_intent(failure):
    pool = await Pool().setup()
    try:
        prepaid = await checkout()
        service = prepaid.service
        service._credential_store = pool.service._credential_store
        service._offers = FakeOffersRepo([pool.offer])
        service._cloud_resolver = pool
        service._routes = pool.selector
        service._create_attempts = ReceiptPort(service._ops, prepaid.servers, pool.events)
        pool.service = service
        pool.failure = failure
        result = await pool.create()
        server = result.server
        assert server.billing_model == BILLING_MODEL_PREPAID_HOURLY_IRT
        snapshot = await prepaid.snapshots.require_snapshot(server.id)
        frozen = deepcopy(server.offer_fingerprint)
        period = await prepaid.periods.get(server.id, server.created_at)
        key = "server-create:hetzner-hourly-confirmation"
        hold = await prepaid.holds.get_by_idempotency(prepaid.wallet.wallet.id, key)
        assert period.usd_minor == snapshot.selling_minor
        assert hold.currency == "IRT" and hold.amount == period.irt_minor
        assert prepaid.quotes.calls == len(prepaid.holds.rows) == 1
        outcome = await service.process_server(server.id)
        assert outcome == ("provisioned" if failure == "capacity" else "outcome-unknown")
        assert [account for account, _ in pool.posts] == (
            ["hz-a", "hz-b"] if failure == "capacity" else ["hz-a"]
        )
        assert hold.status is HoldStatus.CREATED
        assert server.offer_fingerprint == frozen
        assert prepaid.quotes.calls == len(prepaid.holds.rows) == 1
        if failure == "capacity":
            assert pool.posts[0][1] == pool.posts[1][1]
            replay = await pool.create()
            assert replay.replayed and replay.server.id == server.id
            assert prepaid.quotes.calls == len(prepaid.holds.rows) == 1
        else:
            pool.counts["hz-a"] = 5
            await service.reconcile_server(server.id)
            assert len(pool.posts) == 1
            assert server.credential_account_id == "hz-a"
    finally:
        for provider in pool.providers.values():
            await provider.close()
