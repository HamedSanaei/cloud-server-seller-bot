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
* an ELAPSED signal still blocks (expiry is not evidence of recovery); only
  an operator clear / verified positive proof makes the account eligible again;
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
    _accepted_response,
    _Ambiguous,
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
from cloud_platform.providers.errors import ProviderError, ProviderUnavailable

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

    async def test_an_elapsed_signal_still_blocks_checkout(self) -> None:
        """Expiry is NOT recovery: an account whose cooling window elapsed
        without proof must not accept a new order (the provider would refuse it
        again, exactly as it refused 8fc2e573)."""
        capacity = FakeCapacityRepo({ACCOUNT: _limit_reached(expired=True)})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        resolver = _DictResolver({ACCOUNT: FakeHourlyAdapter()})
        service, _, _, _ = _service(
            offers, FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )

        with pytest.raises(HourlyAccountCapacityError):
            await _create(service, offers, idempotency_key="expired-key")
        assert capacity.writes == []

    async def test_an_operator_clear_restores_checkout(self) -> None:
        """A proven recovery (operator clear / verified positive signal) is the
        ONLY transition back to eligibility — and it needs no new refusal."""
        record = _limit_reached(expired=True).recovered()
        capacity = FakeCapacityRepo({ACCOUNT: record})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        resolver = _DictResolver({ACCOUNT: FakeHourlyAdapter()})
        service, _, _, _ = _service(
            offers, FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )

        result = await _create(service, offers, idempotency_key="cleared-key")
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


class _RecordingRepublisher:
    """Records the post-refusal publication refresh, in order.

    The durable capacity write must ALREADY have happened when this runs: a
    second customer confirming in the same second is stopped by the account
    gate, not by the (eventually consistent) catalog.
    """

    def __init__(self, capacity: Any = None, *, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.recovery_calls: list[tuple[str, str]] = []
        self._capacity = capacity
        self._error = error

    async def after_capacity_refusal(self, *, provider_key: str, credential_account_id: str) -> Any:
        if self._capacity is not None:
            assert credential_account_id in self._capacity.records, (
                "the capacity write must land before the catalog refresh"
            )
        self.calls.append((provider_key, credential_account_id))
        if self._error is not None:
            raise self._error
        return None

    async def after_capacity_recovery(
        self, *, provider_key: str, credential_account_id: str
    ) -> Any:
        if self._capacity is not None:
            state = self._capacity.records[credential_account_id].state.value
            assert state == "healthy", "recovery publication must follow the proof"
        self.recovery_calls.append((provider_key, credential_account_id))
        return None


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

    async def test_the_refusal_refreshes_new_order_publication_immediately(self) -> None:
        """The 15-minute catalog cycle is a backstop, not the reaction.

        Production kept advertising sales-org-north for the rest of the cycle
        after the 06:20:02 refusal, which is the window 8fc2e573 was accepted
        in. The moment the refusal is durable, publication is refreshed for
        that account — once, with the exact account that refused.
        """
        capacity = FakeCapacityRepo({})
        republisher = _RecordingRepublisher(capacity)
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, _, _, _ = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            capacity_republisher=republisher,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _pc2031_error()

        assert await service.process_server(server.id) == "failed"
        assert republisher.calls == [(PROVIDER, ACCOUNT)]

    async def test_a_broken_republisher_never_changes_the_operation(self) -> None:
        """Best effort by contract: the catalog refresh must never mask (or
        alter) the provider operation that produced the refusal."""
        capacity = FakeCapacityRepo({})
        republisher = _RecordingRepublisher(error=RuntimeError("catalog sync down"))
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, servers, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            capacity_republisher=republisher,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _pc2031_error()

        assert await service.process_server(server.id) == "failed"
        assert ops.ops[f"server-create:{server.id}"].status.value == "failed"
        assert "PC-2031" in (ops.ops[f"server-create:{server.id}"].error or "")
        assert servers.servers[server.id] is not None
        assert republisher.calls == [(PROVIDER, ACCOUNT)]

    async def test_a_lost_capacity_write_triggers_no_publication_change(self) -> None:
        """No durable evidence means no reason to unpublish anything: the
        periodic sync stays the backstop for that (rare) failure."""

        class _BrokenWrite(FakeCapacityRepo):
            async def record_limit_reached(self, **kwargs: Any) -> Any:
                raise RuntimeError("db write lost")

        republisher = _RecordingRepublisher()
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, _, _, _ = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=_BrokenWrite({}),
            capacity_republisher=republisher,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _pc2031_error()

        assert await service.process_server(server.id) == "failed"
        assert republisher.calls == []

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


class TestExistingResourcesStayManageable:
    """Capacity is scoped to NEW orders: nothing already owned is affected.

    The incident's account must stop receiving NEW business and keep serving
    everything it already owns — including an ambiguous create that still has
    to be resolved through the account pinned on the resource.
    """

    async def test_a_limited_account_still_reconciles_what_it_owns(self) -> None:
        from types import SimpleNamespace

        from cloud_platform.modules.hourly.service import hourly_reference_name

        capacity = FakeCapacityRepo({})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        resolver = _DictResolver({ACCOUNT: cloud})
        service, servers, _, ops = _service(
            offers, FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _Ambiguous("connection lost after POST")
        assert await service.process_server(server.id) == "outcome-unknown"
        # The refusal lands AFTER the resource exists (production's shape: the
        # existing server and its pinned account predate the limit).
        capacity.records[ACCOUNT] = _limit_reached()
        assert capacity.records[ACCOUNT].is_limit_reached() is True

        # ...and the in-flight create it already owns is still resolved through
        # that SAME pinned account (no cross-account replay, no abandon).
        cloud.find_result = SimpleNamespace(
            id="i-9",
            state="RUNNING",
            region="eu-west-3",
            reference=hourly_reference_name(server.id),
            instance_type="lsw.mini",
            image_id="UBUNTU",
        )
        assert await service.reconcile_server(server.id) == "attached"
        assert set(resolver.resolved) == {ACCOUNT}
        assert servers.servers[server.id].provider_server_id == "i-9"
        assert ops.ops[f"server-create:{server.id}"].status.value == "completed"

    async def test_a_settled_signal_never_unresolves_the_pinned_account(self) -> None:
        """Management resolves the credential pinned on the RESOURCE, not the
        capacity state — a settled (unproven) account must stay reachable for
        the servers it already owns."""
        capacity = FakeCapacityRepo({ACCOUNT: _limit_reached(expired=True)})
        cloud = FakeHourlyAdapter()
        resolver = _DictResolver({ACCOUNT: cloud})
        service, _, _, _ = _service(
            FakeOffersRepo([]), FakeHourlyAdapter(), capacity=capacity, resolver=resolver
        )

        settled = capacity.records[ACCOUNT].settled()
        assert settled.state is AccountCapacityState.UNKNOWN_AFTER_LIMIT
        assert settled.blocked_reason() == "unknown-after-limit"
        # The management path resolves the pinned account directly (the same
        # call `reconcile_server` makes) with no capacity consultation at all.
        assert service._adapter_for(PROVIDER, ACCOUNT) is cloud
        assert resolver.resolved == [ACCOUNT]


def _candidate() -> AccountCapacity:
    """The automatic recovery window: ONE real order may prove capacity."""
    return _limit_reached().with_recovery_window_open()


class TestRecoveryCanary:
    """The FIRST REAL CUSTOMER ORDER is the recovery canary (no synthetic
    billable probe exists). Its provider verdict — not time — decides."""

    async def test_an_accepted_canary_proves_recovery_and_republishes(self) -> None:
        capacity = FakeCapacityRepo({ACCOUNT: _candidate()})
        republisher = _RecordingRepublisher(capacity)
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, servers, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            capacity_republisher=republisher,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_result = _accepted_response(server)

        assert await service.process_server(server.id) == "provisioned"

        # Exactly one provider POST, and it held the single canary lease first.
        assert cloud.posts == 1
        assert capacity.canary_attempts == [f"server-create:{server.id}"]
        assert capacity.recovered == [ACCOUNT]
        assert capacity.records[ACCOUNT].state is AccountCapacityState.HEALTHY
        assert capacity.records[ACCOUNT].canary_lease_ref is None
        # Publication through the recovered account is refreshed immediately.
        assert republisher.recovery_calls == [(PROVIDER, ACCOUNT)]
        # The proven acceptance is durable and charged normally (never a
        # synthetic/free probe).
        assert servers.servers[server.id].provider_server_id == "lsw-created"
        assert ops.ops[f"server-create:{server.id}"].status.value == "completed"

    async def test_a_refused_canary_backs_off_without_a_provider_resource(self) -> None:
        capacity = FakeCapacityRepo({ACCOUNT: _candidate()})
        republisher = _RecordingRepublisher(capacity)
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, servers, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            capacity_republisher=republisher,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = _pc2031_error()

        assert await service.process_server(server.id) == "failed"

        record = capacity.records[ACCOUNT]
        assert record.state is AccountCapacityState.LIMIT_REACHED
        assert record.recovery_attempts == 1
        assert record.canary_lease_ref is None
        assert record.next_recovery_attempt_at is not None
        assert capacity.canary_refusals == [
            {
                "account": ACCOUNT,
                "ref": f"server-create:{server.id}",
                "delay_seconds": 900,  # the FIRST backoff step (attempts=0)
                "error_code": "PC-2031",
            }
        ]
        # No provider resource was attached and the operation is terminal:
        # a refused canary is never replayed blindly.
        assert servers.servers[server.id].provider_server_id is None
        assert servers.servers[server.id].state.value == "error"
        assert ops.ops[f"server-create:{server.id}"].status.value == "failed"
        # The account stops being published again, immediately.
        assert republisher.calls == [(PROVIDER, ACCOUNT)]
        assert await service.process_server(server.id) == "skipped"
        assert len(capacity.canary_refusals) == 1

    async def test_a_second_order_never_becomes_a_second_canary(self) -> None:
        """The durable lease serializes the window: while one attempt is in
        flight, another customer fails fast with the capacity answer and NO
        provider POST (no blind retry)."""
        capacity = FakeCapacityRepo({ACCOUNT: _candidate()})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, servers, _, ops = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        first = await _requested_server(service, offers, idempotency_key="canary-1")
        second = await _requested_server(service, offers, idempotency_key="canary-2")
        cloud.create_result = _accepted_response(first)
        # The first attempt is left IN FLIGHT (a transient provider problem
        # requeues it) and therefore KEEPS the lease.
        cloud.create_error = ProviderUnavailable("provider timeout")
        assert await service.process_server(first.id) == "requeued"
        assert capacity.records[ACCOUNT].canary_lease_held() is True

        cloud.create_error = None
        assert await service.process_server(second.id) == "failed"
        assert cloud.posts == 1
        assert capacity.canary_attempts == [f"server-create:{first.id}"]
        assert servers.servers[second.id].provider_server_id is None
        assert ops.ops[f"server-create:{second.id}"].status.value == "failed"

    async def test_a_non_capacity_canary_failure_releases_the_lease(self) -> None:
        """An unrelated provider rejection proves nothing about capacity, so
        the window is not held hostage by it."""
        capacity = FakeCapacityRepo({ACCOUNT: _candidate()})
        offers = FakeOffersRepo([await _usd_offer(provider_account_id=ACCOUNT)])
        cloud = FakeHourlyAdapter()
        service, _, _, _ = _service(
            offers,
            FakeHourlyAdapter(),
            capacity=capacity,
            resolver=_DictResolver({ACCOUNT: cloud}),
        )
        server = await _requested_server(service, offers)
        cloud.create_error = ProviderError("unrelated rejection")

        assert await service.process_server(server.id) == "failed"

        record = capacity.records[ACCOUNT]
        assert record.state is AccountCapacityState.RECOVERY_CANDIDATE
        assert record.canary_lease_ref is None
        assert capacity.canary_refusals == []
        assert capacity.released == [ACCOUNT]
