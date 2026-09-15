"""Tests for the durable credential-account routing repository (mocked session).

The adapter is the only place the platform persists "which credential account
can serve which location". Two properties matter for safety and are pinned
here:

- a location-level observation must never erase the product inventory a
  previous product-level probe recorded (``products_fresh``);
- an unknown persisted state (a newer release wrote it) must degrade to
  ``transient_unknown``/``disabled`` — never to "available".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from cloud_platform.modules.provider_routes.domain import (
    ProviderRoute,
    RouteObservation,
    RouteState,
)
from cloud_platform.modules.provider_routes.repository import (
    SqlAlchemyProviderRouteRepository,
)
from cloud_platform.providers.routing import CredentialAccountState


@pytest.fixture
def session() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    return mock


@pytest.fixture
def repository(session: AsyncMock) -> SqlAlchemyProviderRouteRepository:
    return SqlAlchemyProviderRouteRepository(lambda: session)


def _result(
    *,
    scalars: list[Any] | None = None,
    rows: list[Any] | None = None,
) -> MagicMock:
    """A stubbed SQLAlchemy result supporting both access shapes."""
    result = MagicMock()
    scalar_rows = scalars or []
    result.scalars.return_value.first.return_value = scalar_rows[0] if scalar_rows else None
    result.scalars.return_value.all.return_value = scalar_rows
    result.all.return_value = rows if rows is not None else []
    return result


def _row(**attrs: Any) -> MagicMock:
    row = MagicMock()
    row.provider_key = attrs.get("provider_key", "leaseweb")
    row.credential_account_id = attrs.get("credential_account_id", "lw-1")
    row.location_id = attrs.get("location_id", "FRA-01")
    row.state = attrs.get("state", "eligible_available")
    row.account_state = attrs.get("account_state", "active")
    row.priority = attrs.get("priority", 100)
    row.product_ids = attrs.get("product_ids", ["VPS02_1"])
    row.last_checked_at = attrs.get("last_checked_at")
    row.last_success_at = attrs.get("last_success_at")
    row.last_error_class = attrs.get("last_error_class")
    return row


def _route(**attrs: Any) -> ProviderRoute:
    return ProviderRoute(
        provider_key=attrs.get("provider_key", "leaseweb"),
        credential_account_id=attrs.get("credential_account_id", "lw-1"),
        location_id=attrs.get("location_id", "FRA-01"),
        state=attrs.get("state", RouteState.ELIGIBLE_AVAILABLE),
        priority=attrs.get("priority", 100),
        product_ids=attrs.get("product_ids", ("VPS02_1",)),
        last_checked_at=attrs.get("last_checked_at"),
        last_success_at=attrs.get("last_success_at"),
        last_error_class=attrs.get("last_error_class"),
        account_state=attrs.get("account_state", CredentialAccountState.ACTIVE),
    )


class TestUpsert:
    async def test_inserts_a_new_route_row(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        session.execute.return_value = _result(scalars=[])
        checked = datetime(2026, 9, 14, tzinfo=UTC)
        route = _route(
            state=RouteState.ELIGIBLE_AVAILABLE,
            last_checked_at=checked,
            last_success_at=checked,
        )

        await repository.upsert(route, products_fresh=True)

        session.add.assert_called_once()
        added = session.add.call_args.args[0]
        assert added.provider_key == "leaseweb"
        assert added.credential_account_id == "lw-1"
        assert added.location_id == "FRA-01"
        assert added.state == "eligible_available"
        assert added.account_state == "active"
        assert added.product_ids == ["VPS02_1"]
        assert added.last_success_at == checked
        session.commit.assert_awaited_once()

    async def test_updates_the_existing_route_row(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        row = _row(state="transient_unknown", product_ids=["VPS02_1"])
        session.execute.return_value = _result(scalars=[row])
        checked = datetime(2026, 9, 14, tzinfo=UTC)

        await repository.upsert(
            _route(state=RouteState.ELIGIBLE_AVAILABLE, last_checked_at=checked),
            products_fresh=True,
        )

        session.add.assert_not_called()
        assert row.state == "eligible_available"
        assert row.last_checked_at == checked
        assert row.product_ids == ["VPS02_1"]
        session.commit.assert_awaited_once()

    async def test_location_level_observation_keeps_known_products(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        """A location-only probe must not erase product-level evidence."""
        row = _row(state="eligible_available", product_ids=["VPS02_1", "VPS04_1"])
        session.execute.return_value = _result(scalars=[row])

        await repository.upsert(_route(product_ids=()), products_fresh=False)

        assert row.product_ids == ["VPS02_1", "VPS04_1"]
        session.commit.assert_awaited_once()
        assert row.state == "eligible_available"

    async def test_a_failed_probe_never_overwrites_last_success(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        previous = datetime(2026, 9, 13, tzinfo=UTC)
        row = _row(state="eligible_available", last_success_at=previous)
        session.execute.return_value = _result(scalars=[row])

        await repository.upsert(
            _route(
                state=RouteState.TRANSIENT_UNKNOWN,
                last_checked_at=datetime(2026, 9, 14, tzinfo=UTC),
                last_error_class="LeasewebTimeoutError",
            )
        )

        assert row.state == "transient_unknown"
        assert row.last_success_at == previous
        assert row.last_error_class == "LeasewebTimeoutError"


class TestUpsertObservations:
    async def test_writes_one_row_per_observation(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        session.execute.return_value = _result(scalars=[])
        observations = [
            RouteObservation(
                credential_account_id="lw-2",
                location_id="AMS-01",
                state=RouteState.ELIGIBLE_AVAILABLE,
                product_ids=("VPS02_1",),
                succeeded=True,
            ),
            RouteObservation(
                credential_account_id="lw-1",
                location_id="FRA-01",
                state=RouteState.AUTH_FAILED,
                error_class="LeasewebAuthenticationError",
                succeeded=False,
            ),
        ]
        checked = datetime(2026, 9, 14, tzinfo=UTC)

        written = await repository.upsert_observations(
            provider_key="leaseweb",
            observations=observations,
            priority_of=lambda account: {"lw-1": 100, "lw-2": 200}[account],
            account_state_of=lambda _account: CredentialAccountState.ACTIVE,
            at=checked,
        )

        assert written == 2
        added = [call.args[0] for call in session.add.call_args_list]
        assert [row.credential_account_id for row in added] == ["lw-2", "lw-1"]
        assert added[0].priority == 200
        assert added[0].last_success_at == checked
        assert added[1].last_success_at is None
        assert added[1].state == "auth_failed"

    async def test_an_empty_listing_hides_the_location_without_erasing_products(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        """Only a product-level AVAILABLE listing refreshes the inventory.

        An empty listing keeps the previously observed product ids, which is
        harmless: the state itself (``eligible_empty``) stops the route from
        serving, so no offer can be sold from stale product evidence.
        """
        row = _row(state="eligible_available", product_ids=["VPS02_1"])
        session.execute.return_value = _result(scalars=[row])

        await repository.upsert_observations(
            provider_key="leaseweb",
            observations=[
                RouteObservation(
                    credential_account_id="lw-1",
                    location_id="FRA-01",
                    state=RouteState.ELIGIBLE_EMPTY,
                    product_ids=(),
                    succeeded=True,
                )
            ],
            priority_of=lambda _account: 100,
            account_state_of=lambda _account: CredentialAccountState.ACTIVE,
        )

        assert row.product_ids == ["VPS02_1"]
        assert row.state == "eligible_empty"


class TestReads:
    async def test_lists_routes_for_a_provider(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        checked = datetime(2026, 9, 14, tzinfo=UTC)
        session.execute.return_value = _result(
            scalars=[
                _row(credential_account_id="lw-2", location_id="AMS-01", priority=200),
                _row(credential_account_id="lw-1", last_checked_at=checked),
            ]
        )

        routes = await repository.list_for_provider("leaseweb")

        assert [route.credential_account_id for route in routes] == ["lw-2", "lw-1"]
        assert routes[1].last_checked_at == checked
        assert routes[0].key == ("leaseweb", "lw-2", "AMS-01")

    async def test_lists_routes_for_one_location(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        session.execute.return_value = _result(
            scalars=[_row(credential_account_id="lw-1", priority=100)]
        )

        routes = await repository.list_for_location("leaseweb", "FRA-01")

        assert len(routes) == 1
        assert routes[0].location_id == "FRA-01"
        assert routes[0].is_serving is True

    async def test_unknown_persisted_state_fails_safe(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        session.execute.return_value = _result(
            scalars=[
                _row(state="from_a_newer_release", account_state="also_unknown"),
            ]
        )

        routes = await repository.list_for_provider("leaseweb")

        assert routes[0].state is RouteState.TRANSIENT_UNKNOWN
        assert routes[0].account_state is CredentialAccountState.DISABLED
        assert routes[0].is_serving is False

    async def test_null_products_and_priority_read_as_empty_and_zero(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        session.execute.return_value = _result(
            scalars=[_row(product_ids=None, priority=None, last_error_class=None)]
        )

        routes = await repository.list_for_provider("leaseweb")

        assert routes[0].product_ids == ()
        assert routes[0].priority == 0
        assert routes[0].last_error_class is None

    async def test_account_ids_are_distinct_and_sorted(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        session.execute.return_value = _result(rows=[("lw-3",), ("lw-1",), ("lw-2",)])

        accounts = await repository.account_ids_for_provider("leaseweb")

        assert accounts == ("lw-1", "lw-2", "lw-3")

    async def test_counts_locations_per_account(
        self, repository: SqlAlchemyProviderRouteRepository, session: AsyncMock
    ) -> None:
        session.execute.return_value = _result(rows=[(1,), (2,)])

        count = await repository.count_for_account("leaseweb", "lw-1")

        assert count == 2
