"""Hourly checkout/worker behaviour around provider ACCOUNT CAPACITY (PC-2031).

Production (2026-09-25): ``server-create:5e4bf88c-...`` reached Leaseweb through
``sales-org-north`` and was definitively refused — ``PC-2031`` "Customer limit
reached". The offer, region, image and root disk were all valid; only the
credential ACCOUNT was out of room. These tests pin what must happen then:

* a NEW checkout pinned to such an account is refused with a dedicated error
  (never a generic "offer unavailable"), and nothing is persisted or charged;
* the refusal is remembered as a bounded, admin-visible signal (provider code +
  correlation id + location/type), and losing that write changes nothing;
* an accepted contract is never replayed, never re-routed to another credential,
  and its already-failed operation is never resurrected;
* an expired signal no longer blocks anything;
* the immutable fingerprint keeps the account the catalog selected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_hourly_state_machine import (  # type: ignore[import-not-found]
    FakeCapacityRepo,
    FakeHourlyAdapter,
    FakeOffersRepo,
    _create,
    _DictResolver,
    _requested_server,
    _service,
    _usd_offer,
)

from cloud_platform.modules.hourly.service import HourlyAccountCapacityError
from cloud_platform.modules.provider_capacity.domain import (
    AccountCapacity,
    AccountCapacityState,
)

PROVIDER = "leaseweb"
ACCOUNT = "sales-org-north"


def _pc2031_error() -> Any:
    """The exact production refusal, built through the real error parser."""
    import httpx

    from cloud_platform.providers.leaseweb.errors import (
        error_for_response,
        parse_error_payload,
    )

    payload = parse_error_payload(
        httpx.Response(
            status_code=400,
            json={
                "errorCode": "PC-2031",
                "errorMessage": "Customer limit reached",
                "correlationId": "07376219-7bcd-43d9-a5ea-4128fa57345a",
            },
        )
    )
    return error_for_response(payload, endpoint="/publicCloud/v1/instances")


def _limit_reached(*, expired: bool = False) -> AccountCapacity:
    observed = datetime.now(UTC) - (timedelta(hours=3) if expired else timedelta(minutes=5))
    return AccountCapacity(
        provider_key=PROVIDER,
        credential_account_id=ACCOUNT,
        state=AccountCapacityState.LIMIT_REACHED,
        error_code="PC-2031",
        correlation_id="07376219-7bcd-43d9-a5ea-4128fa57345a",
        location_id="eu-west-3",
        product_id="lsw.mini",
        observations=1,
        observed_at=observed,
        expires_at=observed + timedelta(hours=1),
    )


class TestNewOrderGate:
    async def test_a_limit_reached_account_blocks_a_new_checkout(self) -> None:
        capacity = FakeCapacityRepo({ACCOUNT: _limit_reached()})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        resolver = _DictResolver({ACCOUNT: FakeHourlyAdapter()})
        service, servers, _, _ = _service(
            offers, FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )

        with pytest.raises(HourlyAccountCapacityError):
            await _create(service, offers, idempotency_key="capacity-key")
        # No intent, no reservation, no provider call — and no adapter work.
        assert servers.servers == {}
        assert resolver.resolved == []

    async def test_an_expired_signal_does_not_block_checkout(self) -> None:
        capacity = FakeCapacityRepo({ACCOUNT: _limit_reached(expired=True)})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        resolver = _DictResolver({ACCOUNT: FakeHourlyAdapter()})
        service, _, _, _ = _service(
            offers, FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )

        result = await _create(service, offers, idempotency_key="expired-key")
        assert result.server.credential_account_id == ACCOUNT

    async def test_the_fingerprint_pins_the_account_the_catalog_selected(self) -> None:
        capacity = FakeCapacityRepo({})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id="sales-org-uk")])
        resolver = _DictResolver({"sales-org-uk": FakeHourlyAdapter()})
        service, _, _, _ = _service(
            offers, FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )

        result = await _create(service, offers, idempotency_key="pinned-key")
        assert result.server.credential_account_id == "sales-org-uk"
        assert result.server.offer_fingerprint["credential_account_id"] == "sales-org-uk"

    async def test_an_unreadable_capacity_store_never_blocks_checkout(self) -> None:
        class _Broken:
            async def get(self, provider_key: str, credential_account_id: str) -> Any:
                raise RuntimeError("store down")

        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        resolver = _DictResolver({ACCOUNT: FakeHourlyAdapter()})
        service, _, _, _ = _service(
            offers, FakeHourlyAdapter(), capacity=_Broken(), resolver=resolver
        )

        result = await _create(service, offers, idempotency_key="store-down")
        assert result.server is not None


class TestWorkerCapacityRefusal:
    async def test_the_refusal_records_the_signal_and_fails_the_operation(self) -> None:
        from cloud_platform.modules.compute.domain import ServerLifecycleState

        capacity = FakeCapacityRepo({})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        resolver = _DictResolver({ACCOUNT: cloud})
        service, servers, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            resolver=resolver,
            capacity_ttl_seconds=1800,
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _pc2031_error()

        assert await service.process_server(server.id) == "failed"

        # Exactly one POST, through the account the accepted contract pinned.
        assert cloud.posts == 1
        assert resolver.resolved and set(resolver.resolved) == {ACCOUNT}
        operation = ops.ops[f"server-create:{server.id}"]
        assert operation.status.value == "failed"
        # The operator-facing error keeps the provider code and message.
        assert "PC-2031" in (operation.error or "")
        assert "Customer limit reached" in (operation.error or "")
        # No provider resource exists, and the historical failure is preserved.
        assert servers.servers[server.id].state is ServerLifecycleState.ERROR
        assert servers.servers[server.id].provider_server_id is None

        # The durable, admin-visible capacity signal carries safe facts only.
        assert capacity.writes == [
            {
                "account": ACCOUNT,
                "error_code": "PC-2031",
                "correlation_id": "07376219-7bcd-43d9-a5ea-4128fa57345a",
                "location_id": "eu-west-3",
                "product_id": "lsw.mini",
                "ttl_seconds": 1800,
            }
        ]
        assert capacity.records[ACCOUNT].is_limit_reached() is True

    async def test_a_failed_capacity_request_is_never_replayed(self) -> None:
        capacity = FakeCapacityRepo({})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, _, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _pc2031_error()

        assert await service.process_server(server.id) == "failed"
        # Terminal: the worker never re-POSTs it and never re-opens it.
        assert await service.process_server(server.id) == "skipped"
        assert cloud.posts == 1
        assert ops.ops[f"server-create:{server.id}"].attempts == 1
        assert ops.ops[f"server-create:{server.id}"].status.value == "failed"

    async def test_a_failure_on_one_account_never_posts_through_another(self) -> None:
        capacity = FakeCapacityRepo({})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        north = FakeHourlyAdapter()
        uk = FakeHourlyAdapter()
        resolver = _DictResolver({ACCOUNT: north, "sales-org-uk": uk})
        service, _, _, _ = _service(
            offers, FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )
        server = await _requested_server(service, offers)
        north.create_error = _pc2031_error()

        await service.process_server(server.id)
        await service.process_server(server.id)

        assert north.posts == 1
        # Capacity never re-routes an ACCEPTED contract: the other credential
        # is not even resolved.
        assert uk.posts == 0
        assert "sales-org-uk" not in resolver.resolved

    async def test_losing_the_capacity_write_never_changes_the_outcome(self) -> None:
        class _BrokenWrite(FakeCapacityRepo):
            async def record_limit_reached(self, **kwargs: Any) -> Any:
                raise RuntimeError("db write lost")

        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, servers, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=_BrokenWrite({}),
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _pc2031_error()

        assert await service.process_server(server.id) == "failed"
        assert ops.ops[f"server-create:{server.id}"].status.value == "failed"
        assert servers.servers[server.id] is not None

    async def test_a_transient_provider_failure_is_still_requeued_not_failed(self) -> None:
        """Capacity is definitive; a timeout is not — the states stay distinct."""
        from cloud_platform.providers.errors import ProviderUnavailable

        capacity = FakeCapacityRepo({})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, _, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = ProviderUnavailable("connect timeout before transmission")

        assert await service.process_server(server.id) == "requeued"
        assert capacity.writes == []
        assert ops.ops[f"server-create:{server.id}"].status.value != "failed"
