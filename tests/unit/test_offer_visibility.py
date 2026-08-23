"""Tests for sellable-offer enable/disable (M04-007)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from cloud_platform.modules.catalog.domain import (
    CatalogError,
    OfferNotFoundError,
    OfferRef,
    OfferState,
)
from cloud_platform.modules.catalog.repository import SqlAlchemyCatalogRepository
from cloud_platform.modules.catalog.service import OfferVisibilityService
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
)

REF = OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1")
ADMIN_ID = uuid4()


def _state(enabled: bool = True) -> OfferState:
    return OfferState(
        id=uuid4(),
        ref=REF,
        name="CX22",
        enabled=enabled,
        price_per_quantum=2,
        currency="EUR",
    )


def _user(role: Role = Role.ADMIN) -> User:
    return User(
        id=ADMIN_ID if role is Role.ADMIN else uuid4(),
        username="ops",
        email="ops@example.com",
        role=role,
    )


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class TestOfferRef:
    def test_key_is_stable_identifier(self) -> None:
        assert REF.key == "hetzner/cx22/fsn1"

    @pytest.mark.parametrize("field", ["provider_key", "plan_id", "location_id"])
    def test_empty_fields_rejected(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            OfferRef(
                **{  # type: ignore[arg-type]
                    "provider_key": "hetzner",
                    "plan_id": "cx22",
                    "location_id": "fsn1",
                    field: "  ",
                }
            )

    def test_ref_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            REF.plan_id = "other"  # type: ignore[misc]

    def test_state_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            _state().enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _service(catalog: AsyncMock, audit: AsyncMock) -> OfferVisibilityService:
    return OfferVisibilityService(catalog, audit)  # type: ignore[arg-type]


def _audit_events(audit: AsyncMock) -> list:
    return [c.args[0] for c in audit.append.call_args_list]


class TestSetOfferVisibility:
    async def test_admin_hides_enabled_offer(self) -> None:
        catalog = AsyncMock()
        catalog.get_offer = AsyncMock(return_value=_state(enabled=True))
        catalog.set_offer_enabled = AsyncMock()
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        result = await _service(catalog, audit).set_offer_visibility(
            ref=REF, enabled=False, actor=_user(), reason="abuse case 7"
        )

        assert result.enabled is False
        assert result.name == "CX22"
        catalog.set_offer_enabled.assert_awaited_once_with(REF, False)
        event = _audit_events(audit)[0]
        assert event.action == "catalog.offer_hide"
        assert event.resource_type == "catalog_offer"
        assert event.resource_id == REF.key
        assert event.actor_type.value == "admin"
        assert event.actor_id == ADMIN_ID
        assert event.reason == "abuse case 7"
        assert event.metadata == {
            "provider": "hetzner",
            "plan": "cx22",
            "location": "fsn1",
        }

    async def test_admin_shows_hidden_offer(self) -> None:
        catalog = AsyncMock()
        catalog.get_offer = AsyncMock(return_value=_state(enabled=False))
        catalog.set_offer_enabled = AsyncMock()
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        result = await _service(catalog, audit).set_offer_visibility(
            ref=REF, enabled=True, actor=_user(), reason="abuse cleared"
        )

        assert result.enabled is True
        catalog.set_offer_enabled.assert_awaited_once_with(REF, True)
        assert _audit_events(audit)[0].action == "catalog.offer_show"

    async def test_idempotent_replay_emits_nothing(self) -> None:
        catalog = AsyncMock()
        catalog.get_offer = AsyncMock(return_value=_state(enabled=False))
        catalog.set_offer_enabled = AsyncMock()
        audit = AsyncMock()

        result = await _service(catalog, audit).set_offer_visibility(
            ref=REF, enabled=False, actor=_user(), reason="re-run"
        )

        assert result.enabled is False
        catalog.set_offer_enabled.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_unknown_offer_rejected(self) -> None:
        catalog = AsyncMock()
        catalog.get_offer = AsyncMock(return_value=None)
        catalog.set_offer_enabled = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(OfferNotFoundError, match="not found"):
            await _service(catalog, audit).set_offer_visibility(
                ref=REF, enabled=False, actor=_user(), reason="r"
            )
        catalog.set_offer_enabled.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_non_admin_denied_before_any_io(self) -> None:
        catalog = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(PermissionDeniedError):
            await _service(catalog, audit).set_offer_visibility(
                ref=REF, enabled=False, actor=_user(Role.USER), reason="r"
            )
        catalog.get_offer.assert_not_awaited()
        audit.append.assert_not_awaited()

    async def test_empty_reason_rejected(self) -> None:
        catalog = AsyncMock()
        audit = AsyncMock()

        with pytest.raises(CatalogError, match="reason"):
            await _service(catalog, audit).set_offer_visibility(
                ref=REF, enabled=False, actor=_user(), reason="   "
            )
        catalog.get_offer.assert_not_awaited()
        catalog.set_offer_enabled.assert_not_awaited()
        audit.append.assert_not_awaited()


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


def _provider_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.name = "hetzner"
    return row


def _catalog_row(enabled: bool = True) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.name = "CX22"
    row.provider_plan_id = "cx22"
    row.provider_location_id = "fsn1"
    row.enabled = enabled
    row.price_per_quantum = 2
    row.currency = "EUR"
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.commit = AsyncMock()
    return mock


def _repo(db: AsyncMock) -> SqlAlchemyCatalogRepository:
    return SqlAlchemyCatalogRepository(lambda: db)  # type: ignore[arg-type]


class TestOfferRepository:
    async def test_get_offer_maps_row(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: _provider_row())),
                MagicMock(scalars=lambda: MagicMock(first=lambda: _catalog_row(True))),
            ]
        )

        state = await _repo(db).get_offer(REF)

        assert state is not None
        assert state.ref == REF
        assert state.name == "CX22"
        assert state.enabled is True
        assert state.price_per_quantum == 2
        assert state.currency == "EUR"

    async def test_get_offer_unknown_provider_returns_none(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )

        assert await _repo(db).get_offer(REF) is None
        db.execute.assert_awaited_once()  # provider lookup only, no catalog query

    async def test_get_offer_unknown_combination_returns_none(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: _provider_row())),
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
            ]
        )

        assert await _repo(db).get_offer(REF) is None

    async def test_set_offer_enabled_updates_row(self, db: AsyncMock) -> None:
        row = _catalog_row(True)
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: _provider_row())),
                MagicMock(scalars=lambda: MagicMock(first=lambda: row)),
            ]
        )

        await _repo(db).set_offer_enabled(REF, False)

        assert row.enabled is False
        db.commit.assert_awaited_once()

    async def test_set_offer_enabled_unknown_provider_raises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )

        with pytest.raises(LookupError, match="not found"):
            await _repo(db).set_offer_enabled(REF, False)
        db.commit.assert_not_awaited()

    async def test_set_offer_enabled_unknown_row_raises(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalars=lambda: MagicMock(first=lambda: _provider_row())),
                MagicMock(scalars=lambda: MagicMock(first=lambda: None)),
            ]
        )

        with pytest.raises(LookupError, match="not found"):
            await _repo(db).set_offer_enabled(REF, True)
