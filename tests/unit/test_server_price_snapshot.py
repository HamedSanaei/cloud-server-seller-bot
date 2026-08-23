"""Tests for immutable server price snapshots (M06-002).

Includes the acceptance proof: a snapshot's price is unaffected by later
catalog or price-book changes.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from cloud_platform.modules.pricing.domain import (
    MarginRule,
    MissingPriceSnapshotError,
    OfferCost,
    SellingPrice,
    ServerPriceSnapshot,
    SnapshotAlreadyExistsError,
    snapshot_from_selling_price,
)
from cloud_platform.modules.pricing.repository import SqlAlchemyServerPriceSnapshotRepository
from cloud_platform.modules.pricing.service import ServerPriceSnapshotService
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
)

SERVER_ID = uuid4()
ADMIN_ID = uuid4()
T0 = datetime(2026, 8, 23, 0, 0, tzinfo=UTC)


def _user(role: Role = Role.ADMIN) -> User:
    return User(
        id=ADMIN_ID if role is Role.ADMIN else uuid4(),
        username="ops",
        email="ops@example.com",
        role=role,
    )


def _offer(**overrides: object) -> OfferCost:
    defaults: dict[str, object] = {
        "provider_key": "hetzner",
        "plan_id": "cx22",
        "location_id": "fsn1",
        "cost_minor": 100,
        "currency": "EUR",
    }
    defaults.update(overrides)
    return OfferCost(**defaults)  # type: ignore[arg-type]


def _price(cost_minor: int = 100, factor: str = "1.07") -> SellingPrice:
    rule = MarginRule("*", "*", "*", Decimal(factor))
    return SellingPrice(
        offer=_offer(cost_minor=cost_minor),
        selling_minor=round(cost_minor * Decimal(factor)),
        rule=rule,
        book_name="retail-eur",
        version=1,
        priced_at=T0,
    )


class TestSnapshotDomain:
    def test_snapshot_from_selling_price_copies_fields(self) -> None:
        price = _price()
        snap = snapshot_from_selling_price(SERVER_ID, price)
        assert snap.server_id == SERVER_ID
        assert snap.offer == price.offer
        assert snap.selling_minor == price.selling_minor
        assert snap.book_name == "retail-eur"
        assert snap.book_version == 1
        assert snap.rule == price.rule
        assert snap.priced_at == T0

    def test_invalid_snapshots_rejected(self) -> None:
        with pytest.raises(ValueError, match="selling_minor"):
            ServerPriceSnapshot(
                server_id=SERVER_ID,
                offer=_offer(),
                selling_minor=-1,
                book_name="b",
                book_version=1,
                rule=MarginRule("*", "*", "*", Decimal("1")),
                priced_at=T0,
            )
        with pytest.raises(ValueError, match="book_version"):
            ServerPriceSnapshot(
                server_id=SERVER_ID,
                offer=_offer(),
                selling_minor=1,
                book_name="b",
                book_version=0,
                rule=MarginRule("*", "*", "*", Decimal("1")),
                priced_at=T0,
            )
        with pytest.raises(ValueError, match="book_name"):
            ServerPriceSnapshot(
                server_id=SERVER_ID,
                offer=_offer(),
                selling_minor=1,
                book_name="  ",
                book_version=1,
                rule=MarginRule("*", "*", "*", Decimal("1")),
                priced_at=T0,
            )
        with pytest.raises(ValueError, match="timezone-aware"):
            ServerPriceSnapshot(
                server_id=SERVER_ID,
                offer=_offer(),
                selling_minor=1,
                book_name="b",
                book_version=1,
                rule=MarginRule("*", "*", "*", Decimal("1")),
                priced_at=datetime(2026, 8, 23),
            )

    def test_snapshot_is_frozen(self) -> None:
        snap = snapshot_from_selling_price(SERVER_ID, _price())
        with pytest.raises(FrozenInstanceError):
            snap.selling_minor = 999  # type: ignore[misc]


class _InMemorySnapshotRepo:
    """Tiny in-memory repo standing in for the DB in service tests."""

    def __init__(self) -> None:
        self.stored: ServerPriceSnapshot | None = None
        self.calls = 0

    async def create(self, snapshot: ServerPriceSnapshot) -> ServerPriceSnapshot:
        self.calls += 1
        if self.stored is not None:
            raise SnapshotAlreadyExistsError("already priced")
        self.stored = snapshot
        return snapshot

    async def get(self, server_id: UUID) -> ServerPriceSnapshot | None:
        if self.stored is not None and self.stored.server_id == server_id:
            return self.stored
        return None


def _service(repo: _InMemorySnapshotRepo, audit: AsyncMock) -> ServerPriceSnapshotService:
    return ServerPriceSnapshotService(repo, audit)  # type: ignore[arg-type]


class TestSnapshotService:
    async def test_system_create_audited(self) -> None:
        repo = _InMemorySnapshotRepo()
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        created = await _service(repo, audit).create_snapshot(server_id=SERVER_ID, price=_price())

        assert created.selling_minor == 107
        assert created.id is None  # in-memory stand-in, id assigned by DB
        assert repo.calls == 1
        event = audit.append.call_args[0][0]
        assert event.action == "pricing.snapshot_server"
        assert event.resource_type == "server"
        assert event.resource_id == str(SERVER_ID)
        assert event.actor_type.value == "system"
        assert event.metadata["book"] == "retail-eur"
        assert event.metadata["selling_minor"] == 107

    async def test_admin_create_audited_with_reason(self) -> None:
        repo = _InMemorySnapshotRepo()
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        await _service(repo, audit).create_snapshot(
            server_id=SERVER_ID, price=_price(), actor=_user(), reason="manual re-price entry"
        )
        event = audit.append.call_args[0][0]
        assert event.actor_type.value == "admin"
        assert event.actor_id == ADMIN_ID
        assert event.reason == "manual re-price entry"

    async def test_non_admin_denied(self) -> None:
        repo = _InMemorySnapshotRepo()
        audit = AsyncMock()

        with pytest.raises(PermissionDeniedError):
            await _service(repo, audit).create_snapshot(
                server_id=SERVER_ID, price=_price(), actor=_user(Role.USER)
            )
        assert repo.calls == 0
        audit.append.assert_not_awaited()

    async def test_admin_empty_reason_rejected(self) -> None:
        repo = _InMemorySnapshotRepo()
        audit = AsyncMock()

        with pytest.raises(ValueError, match="reason"):
            await _service(repo, audit).create_snapshot(
                server_id=SERVER_ID, price=_price(), actor=_user(), reason="  "
            )
        assert repo.calls == 0

    async def test_duplicate_create_raises_without_audit(self) -> None:
        repo = _InMemorySnapshotRepo()
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)

        await _service(repo, audit).create_snapshot(server_id=SERVER_ID, price=_price())
        with pytest.raises(SnapshotAlreadyExistsError):
            await _service(repo, audit).create_snapshot(server_id=SERVER_ID, price=_price())
        assert repo.calls == 2
        # only the first creation was audited
        assert audit.append.await_count == 1

    async def test_get_and_require(self) -> None:
        repo = _InMemorySnapshotRepo()
        audit = AsyncMock()
        service = _service(repo, audit)

        assert await service.get_snapshot(SERVER_ID) is None
        with pytest.raises(MissingPriceSnapshotError):
            await service.require_snapshot(SERVER_ID)

        await service.create_snapshot(server_id=SERVER_ID, price=_price())
        snap = await service.require_snapshot(SERVER_ID)
        assert snap is not None
        assert snap.selling_minor == 107


class TestAcceptanceHistoryUnaffected:
    """Acceptance: historical price unaffected by catalog changes."""

    async def test_snapshot_survives_price_book_and_catalog_changes(self) -> None:
        repo = _InMemorySnapshotRepo()
        audit = AsyncMock()
        audit.append = AsyncMock(side_effect=lambda e: e)
        service = _service(repo, audit)

        # Provisioning at T0: book v1 (factor 1.07), cost 100 -> 107.
        await service.create_snapshot(server_id=SERVER_ID, price=_price(factor="1.07"))

        # Afterwards the catalog changes: provider cost rises to 200 and a new
        # price book version (v2, factor 1.5) is published.
        newer_catalog_price = SellingPrice(
            offer=_offer(cost_minor=200),
            selling_minor=300,
            rule=MarginRule("*", "*", "*", Decimal("1.5")),
            book_name="retail-eur",
            version=2,
            priced_at=T0,
        )

        # The server's price must still be the snapshot — 107, not 300.
        snap = await service.require_snapshot(SERVER_ID)
        assert snap.selling_minor == 107
        assert snap.book_version == 1
        assert snap.offer.cost_minor == 100

        # And the snapshot is immutable: a second snapshot can never replace it.
        with pytest.raises(SnapshotAlreadyExistsError):
            await service.create_snapshot(server_id=SERVER_ID, price=newer_catalog_price)
        still = await service.require_snapshot(SERVER_ID)
        assert still.selling_minor == 107


# ---------------------------------------------------------------------------
# Repository (mocked session)
# ---------------------------------------------------------------------------


def _snapshot_row(**overrides: object) -> MagicMock:
    row = MagicMock()
    row.id = uuid4()
    row.server_id = SERVER_ID
    row.provider_key = "hetzner"
    row.plan_id = "cx22"
    row.location_id = "fsn1"
    row.currency = "EUR"
    row.cost_minor = 100
    row.selling_minor = 107
    row.book_name = "retail-eur"
    row.book_version = 1
    row.margin_rule = {
        "provider": "*",
        "plan": "*",
        "location": "*",
        "margin_factor": "1.07",
        "fixed_minor": 0,
    }
    row.priced_at = T0
    row.created_at = T0
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


@pytest.fixture
def db() -> AsyncMock:
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.add = MagicMock()
    mock.commit = AsyncMock()
    mock.rollback = AsyncMock()
    return mock


def _repo(db: AsyncMock) -> SqlAlchemyServerPriceSnapshotRepository:
    return SqlAlchemyServerPriceSnapshotRepository(lambda: db)  # type: ignore[arg-type]


class TestSnapshotRepository:
    async def test_create_persists_flattened_offer(self, db: AsyncMock) -> None:
        db.refresh = AsyncMock(side_effect=lambda r: setattr(r, "id", uuid4()))
        snap = snapshot_from_selling_price(SERVER_ID, _price())

        created = await _repo(db).create(snap)

        assert created.server_id == SERVER_ID
        assert created.selling_minor == 107
        assert created.offer == _offer()
        assert created.rule.margin_factor == Decimal("1.07")
        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        row = db.add.call_args[0][0]
        assert row.server_id == SERVER_ID
        assert row.cost_minor == 100
        assert row.selling_minor == 107
        assert row.book_name == "retail-eur"
        assert row.book_version == 1
        assert row.margin_rule == {
            "provider": "*",
            "plan": "*",
            "location": "*",
            "margin_factor": "1.07",
            "fixed_minor": 0,
        }

    async def test_create_duplicate_raises(self, db: AsyncMock) -> None:
        db.commit = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("uq")))
        snap = snapshot_from_selling_price(SERVER_ID, _price())

        with pytest.raises(SnapshotAlreadyExistsError, match="already has a price snapshot"):
            await _repo(db).create(snap)
        db.rollback.assert_awaited_once()

    async def test_get_maps_row(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: _snapshot_row()))
        )
        snap = await _repo(db).get(SERVER_ID)
        assert snap is not None
        assert snap.server_id == SERVER_ID
        assert snap.selling_minor == 107
        assert snap.priced_at.tzinfo is not None

    async def test_get_returns_none_when_missing(self, db: AsyncMock) -> None:
        db.execute = AsyncMock(
            return_value=MagicMock(scalars=lambda: MagicMock(first=lambda: None))
        )
        assert await _repo(db).get(SERVER_ID) is None
