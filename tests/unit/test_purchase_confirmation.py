"""Tests for the purchase confirmation view (M08-005).

Acceptance: shows exact price policy and wallet impact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.catalog.domain import CatalogOffer
from cloud_platform.modules.catalog.service import (
    PurchaseConfirmationError,
    PurchaseConfirmationService,
)
from cloud_platform.modules.navigation.domain import decode_callback
from cloud_platform.modules.pricing.domain import (
    MarginRule,
    NoActiveVersionError,
    OfferCost,
    SellingPrice,
)
from cloud_platform.modules.wallet.domain import Wallet

SIGNING_KEY = "test-signing-key"
BOOK = "retail-eur"
OFFER_ID = uuid4()
USER_ID = uuid4()
T0 = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _offer(
    *,
    cost: int = 700,
    enabled: bool = True,
    vcpu: int = 2,
) -> CatalogOffer:
    return CatalogOffer(
        id=OFFER_ID,
        provider_key="hetzner",
        plan_id="cx22",
        location_id="fsn1",
        name="CX22",
        architecture="x86",
        vcpu=vcpu,
        memory_mb=4096,
        disk_gb=40,
        currency="EUR",
        price_per_quantum=cost,
        quantum_seconds=3600,
        enabled=enabled,
    )


def _rule(cap: int | None = None) -> MarginRule:
    return MarginRule(
        provider="hetzner",
        plan="cx22",
        location="fsn1",
        margin_factor=Decimal("1.43"),
        monthly_cap_minor=cap,
    )


class FakeBookService:
    """Duck of PriceBookService: derives the price the create command would."""

    def __init__(self, price: SellingPrice | None = None, error: Exception | None = None) -> None:
        self._price = price
        self._error = error
        self.requests: list[tuple[str, OfferCost, datetime]] = []

    def make_price(self, cost: OfferCost) -> SellingPrice:
        return SellingPrice(
            offer=cost,
            selling_minor=cost.cost_minor * 143 // 100,
            rule=_rule(),
            book_name=BOOK,
            version=3,
            priced_at=T0,
        )

    async def sell_price(self, *, book_name: str, offer: OfferCost, at: datetime) -> SellingPrice:
        self.requests.append((book_name, offer, at))
        if self._error is not None:
            raise self._error
        if self._price is not None:
            return self._price
        return self.make_price(offer)


class FakeCatalog:
    def __init__(self, offer: CatalogOffer | None) -> None:
        self._offer = offer

    async def get_by_id(self, offer_id: UUID) -> CatalogOffer | None:
        return self._offer if self._offer.id == offer_id else None


class FakeWalletRepo:
    def __init__(self, wallet: Wallet | None) -> None:
        self._wallet = wallet

    async def get(self, user_id: UUID) -> Wallet | None:
        return self._wallet


def _service(
    offer: CatalogOffer | None,
    wallet: Wallet | None,
    book: FakeBookService | None = None,
) -> PurchaseConfirmationService:
    books = book or FakeBookService()

    class _Books:
        async def sell_price(self, **kw):
            return await books.sell_price(**kw)

    return PurchaseConfirmationService(
        FakeCatalog(offer), _Books(), FakeWalletRepo(wallet), SIGNING_KEY, BOOK
    )


def _price_for(cost_minor: int, cap: int | None = None) -> SellingPrice:
    cost = OfferCost("hetzner", "cx22", "fsn1", cost_minor, "EUR")
    return SellingPrice(
        offer=cost,
        selling_minor=cost_minor * 143 // 100,
        rule=_rule(cap),
        book_name=BOOK,
        version=3,
        priced_at=T0,
    )


class TestExactPricePolicy:
    async def test_shows_book_derived_price(self) -> None:
        # cost 700, margin 1.43 -> 1001 (700*1.43 = 1001.0)
        view = await _service(_offer(cost=700), None).confirmation(
            USER_ID, "hetzner", "fsn1", OFFER_ID, "ubuntu-24.04"
        )

        p = view.policy
        assert p.selling_minor_per_quantum == 1001
        assert p.currency == "EUR"
        assert p.quantum_seconds == 3600
        assert p.margin_factor == Decimal("1.43")
        assert p.book_name == BOOK
        assert p.book_version == 3
        assert p.monthly_cap_minor is None
        # the hold is exactly the first quantum (what create_server holds)
        assert p.hold_minor == 1001
        text = view.render()
        assert "10.01 EUR/60min" in text
        assert "v3" in text

    async def test_monthly_cap_shown_when_rule_has_one(self) -> None:
        price = _price_for(700, cap=50_000)
        view = await _service(_offer(cost=700), None, FakeBookService(price=price)).confirmation(
            USER_ID, "hetzner", "fsn1", OFFER_ID
        )
        assert view.policy.monthly_cap_minor == 50_000
        assert "monthly cap: 500.00 EUR" in view.render()

    async def test_price_book_errors_propagate(self) -> None:
        book = FakeBookService(error=NoActiveVersionError("no version effective"))
        with pytest.raises(NoActiveVersionError):
            await _service(_offer(), None, book).confirmation(USER_ID, "hetzner", "fsn1", OFFER_ID)

    async def test_offer_cost_built_from_catalog_row(self) -> None:
        book = FakeBookService()
        await _service(_offer(cost=700), None, book).confirmation(
            USER_ID, "hetzner", "fsn1", OFFER_ID
        )
        (book_name, cost, _at) = book.requests[0]
        assert book_name == BOOK
        assert (cost.provider_key, cost.plan_id, cost.location_id, cost.cost_minor) == (
            "hetzner",
            "cx22",
            "fsn1",
            700,
        )


class TestWalletImpact:
    async def test_sufficient_wallet(self) -> None:
        wallet = Wallet(USER_ID, id=uuid4(), balance=10_000)
        view = await _service(_offer(cost=700), wallet).confirmation(
            USER_ID, "hetzner", "fsn1", OFFER_ID
        )

        w = view.wallet
        assert w.has_wallet is True
        assert w.balance_minor == 10_000
        assert w.hold_minor == 1001
        assert w.balance_after_hold_minor == 8_999
        assert w.sufficient is True
        assert "(ok)" in view.render()

    async def test_insufficient_wallet_flagged_not_enforced(self) -> None:
        wallet = Wallet(USER_ID, id=uuid4(), balance=500)
        view = await _service(_offer(cost=700), wallet).confirmation(
            USER_ID, "hetzner", "fsn1", OFFER_ID
        )

        w = view.wallet
        assert w.sufficient is False
        assert w.balance_after_hold_minor == 500 - 1001  # shown, even negative
        assert "(insufficient!)" in view.render()

    async def test_exact_balance_is_sufficient(self) -> None:
        wallet = Wallet(USER_ID, id=uuid4(), balance=1001)
        view = await _service(_offer(cost=700), wallet).confirmation(
            USER_ID, "hetzner", "fsn1", OFFER_ID
        )
        assert view.wallet.sufficient is True
        assert view.wallet.balance_after_hold_minor == 0

    async def test_no_wallet_shown(self) -> None:
        view = await _service(_offer(), None).confirmation(USER_ID, "hetzner", "fsn1", OFFER_ID)
        assert view.wallet.has_wallet is False
        assert view.wallet.sufficient is False
        assert "wallet: none" in view.render()


class TestScreenContext:
    async def test_offer_must_match_location(self) -> None:
        with pytest.raises(PurchaseConfirmationError, match="not found"):
            await _service(_offer(), None).confirmation(USER_ID, "hetzner", "nbg1", OFFER_ID)

    async def test_unknown_offer(self) -> None:
        with pytest.raises(PurchaseConfirmationError):
            await _service(_offer(), None).confirmation(USER_ID, "hetzner", "fsn1", uuid4())

    async def test_disabled_offer(self) -> None:
        with pytest.raises(PurchaseConfirmationError, match="not sellable"):
            await _service(_offer(enabled=False), None).confirmation(
                USER_ID, "hetzner", "fsn1", OFFER_ID
            )

    async def test_image_and_callbacks(self) -> None:
        view = await _service(_offer(), None).confirmation(
            USER_ID, "hetzner", "fsn1", OFFER_ID, "ubuntu-24.04"
        )

        assert view.image_id == "ubuntu-24.04"
        confirm = decode_callback(view.confirm_callback, SIGNING_KEY)
        assert confirm.flow == "buy"
        assert confirm.screen == "confirm"
        assert confirm.args == ("hetzner", "fsn1", str(OFFER_ID), "ubuntu-24.04")

        back = decode_callback(view.back_callback, SIGNING_KEY)
        assert back.screen == "os"

        cancel = decode_callback(view.cancel_callback, SIGNING_KEY)
        assert (cancel.flow, cancel.screen) == ("main", "menu")

    async def test_no_image_uses_none_placeholder(self) -> None:
        view = await _service(_offer(), None).confirmation(USER_ID, "hetzner", "fsn1", OFFER_ID)
        assert view.image_id is None
        confirm = decode_callback(view.confirm_callback, SIGNING_KEY)
        assert confirm.args[3] == "none"


class TestServiceValidation:
    def test_empty_signing_key_rejected(self) -> None:
        with pytest.raises(ValueError):
            PurchaseConfirmationService(
                FakeCatalog(None), FakeBookService(), FakeWalletRepo(None), "", BOOK
            )

    def test_empty_book_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            PurchaseConfirmationService(
                FakeCatalog(None), FakeBookService(), FakeWalletRepo(None), SIGNING_KEY, "  "
            )
