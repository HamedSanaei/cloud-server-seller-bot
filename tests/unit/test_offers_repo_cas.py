"""Compare-and-swap pricing/publication contract for sellable offers.

``set_auto_price_if_current``, ``publish_if_current``,
``record_auto_pricing_failure_if_current`` and ``get_by_ref`` are pinned with
a scripted session double: ``execute`` serves queued results in call order
(exactly as the implementation issues them) while ``commit``/``rollback`` are
counted. Pricing payloads come from the real ``CatalogOfferPricer`` so the
provenance assertions exercise true behavior, never a mocked outcome.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from cloud_platform.modules.fx.domain import FxReferenceQuote
from cloud_platform.modules.fx.service import ReferenceRateResolution
from cloud_platform.modules.offers.domain import OfferNotFoundError, PricingPolicy, SellableOffer
from cloud_platform.modules.offers.pricing import CatalogOfferPricer, PricedOffer
from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository


class _StubRates:
    """Deterministic exact-rate source; priced through the real pricer."""

    def __init__(self, rate: Decimal) -> None:
        self.rate = rate

    async def get_rate(
        self, base: str, quote: str, *, allow_catalog_stale: bool = False
    ) -> ReferenceRateResolution:
        now = datetime.now(UTC)
        return ReferenceRateResolution(
            FxReferenceQuote(
                base_currency=base,
                quote_currency=quote,
                rate=self.rate,
                source="frankfurter",
                source_market=f"{base}/{quote}",
                provider_date=now.date(),
                observed_at=now,
                expires_at=now + timedelta(hours=1),
            ),
            stale=False,
        )


class _Result:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def scalar_one_or_none(self) -> Any:
        if isinstance(self._payload, list):
            return self._payload[0] if self._payload else None
        return self._payload

    def scalars(self) -> _Scalars:
        if isinstance(self._payload, list):
            return _Scalars(list(self._payload))
        if self._payload is None:
            return _Scalars([])
        return _Scalars([self._payload])


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _ScriptedSession:
    """Session double serving queued execute results in call order."""

    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.commits = 0
        self.rollbacks = 0
        self.executes = 0

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, stmt: Any) -> _Result:
        self.executes += 1
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, row: Any) -> None:
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1


def _repo(session: _ScriptedSession) -> SqlAlchemySellableOfferRepository:
    return SqlAlchemySellableOfferRepository(lambda: session)  # type: ignore[arg-type]


def _row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid4(),
        "provider_key": "leaseweb",
        "product_id": "plan-a",
        "location_id": "eu-west",
        "name": "Plan A",
        "vcpu": 2,
        "ram_gb": 4,
        "disk_gb": 80,
        "traffic": None,
        "provider_cost_minor": 1000,
        "provider_cost_currency": "EUR",
        "selling_price_minor": 0,
        "selling_currency": "EUR",
        "billing_parameters": {"provider_monthly_rate": "10.00"},
        "technical_metadata": {},
        "pricing_metadata": {},
        "billing_model": "prepaid_monthly_fixed",
        "provider_available": True,
        "enabled": False,
        "operator_disabled": False,
        "auto_priced": True,
        "created_at": None,
        "updated_at": None,
        "provider_account_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _domain_offer() -> SellableOffer:
    return SellableOffer(
        id=uuid4(),
        provider_key="leaseweb",
        product_id="plan-a",
        location_id="eu-west",
        name="Plan A",
        vcpu=2,
        ram_gb=4,
        disk_gb=80,
        traffic=None,
        provider_cost_minor=1000,
        provider_cost_currency="EUR",
        selling_price_minor=0,
        selling_currency="EUR",
        billing_parameters={"provider_monthly_rate": "10.00"},
        billing_model="prepaid_monthly_fixed",
        provider_available=True,
        enabled=False,
        auto_priced=True,
    )


async def _auto_price() -> PricedOffer:
    """Real auto-price payload: 10.00 EUR * 1.10 * 1.10 = 12.10 -> 1210."""
    return await CatalogOfferPricer(_StubRates(Decimal("1.10")), "USD").price_auto(
        _domain_offer(), PricingPolicy(markup_percent=10)
    )


async def _priced_row_async() -> SimpleNamespace:
    priced = await _auto_price()
    assert priced.selling_price_minor == 1210
    return _row(
        selling_price_minor=priced.selling_price_minor,
        selling_currency="USD",
        pricing_metadata=dict(priced.pricing_metadata),
    )


class TestSetAutoPriceIfCurrent:
    async def test_applies_on_matching_expectations(self) -> None:
        priced = await _auto_price()
        row = _row()
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.set_auto_price_if_current(
            row.id,
            expected_cost_minor=1000,
            expected_cost_currency="EUR",
            selling_price_minor=priced.selling_price_minor,
            selling_currency="USD",
            pricing_metadata=dict(priced.pricing_metadata),
            expected_provider_rate="10.00",
        )

        assert result is not None
        assert result.selling_price_minor == 1210
        assert result.selling_currency == "USD"
        assert row.selling_price_minor == 1210
        assert session.commits == 1
        assert session.executes == 1

    async def test_returns_none_on_cost_drift(self) -> None:
        priced = await _auto_price()
        row = _row()
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.set_auto_price_if_current(
            row.id,
            expected_cost_minor=999,  # drifted observation
            expected_cost_currency="EUR",
            selling_price_minor=priced.selling_price_minor,
            selling_currency="USD",
            pricing_metadata=dict(priced.pricing_metadata),
            expected_provider_rate="10.00",
        )

        assert result is None
        assert row.selling_price_minor == 0
        assert session.commits == 0

    async def test_raises_on_provenance_failure(self) -> None:
        row = _row()
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        try:
            await repo.set_auto_price_if_current(
                row.id,
                expected_cost_minor=1000,
                expected_cost_currency="EUR",
                selling_price_minor=1210,
                selling_currency="USD",
                pricing_metadata={},  # CAS guards pass, provenance cannot
                expected_provider_rate="10.00",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("provenance-free auto price must raise")
        assert session.commits == 0

    async def test_raises_when_row_missing(self) -> None:
        priced = await _auto_price()
        session = _ScriptedSession([_Result(None)])
        repo = _repo(session)

        try:
            await repo.set_auto_price_if_current(
                uuid4(),
                expected_cost_minor=1000,
                expected_cost_currency="EUR",
                selling_price_minor=priced.selling_price_minor,
                selling_currency="USD",
                pricing_metadata=dict(priced.pricing_metadata),
            )
        except OfferNotFoundError:
            pass
        else:
            raise AssertionError("missing row must raise OfferNotFoundError")


class TestPublishIfCurrent:
    async def test_enables_on_match(self) -> None:
        row = await _priced_row_async()
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.publish_if_current(
            row.id,
            expected_price_minor=1210,
            expected_currency="USD",
            expected_cost_minor=1000,
            expected_cost_currency="EUR",
        )

        assert result is not None
        assert result.enabled is True
        assert row.enabled is True
        assert session.commits == 1

    async def test_returns_none_when_operator_disabled(self) -> None:
        row = await _priced_row_async()
        row.operator_disabled = True
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.publish_if_current(
            row.id,
            expected_price_minor=1210,
            expected_currency="USD",
            expected_cost_minor=1000,
            expected_cost_currency="EUR",
        )

        assert result is None
        assert row.enabled is False
        assert session.commits == 0

    async def test_returns_none_on_price_drift(self) -> None:
        row = await _priced_row_async()
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.publish_if_current(
            row.id,
            expected_price_minor=9999,  # drifted price
            expected_currency="USD",
            expected_cost_minor=1000,
            expected_cost_currency="EUR",
        )

        assert result is None
        assert row.enabled is False
        assert session.commits == 0


class TestRecordAutoPricingFailureIfCurrent:
    async def test_preserves_still_valid_price(self) -> None:
        row = await _priced_row_async()
        expected_metadata = dict(row.pricing_metadata)
        failure_metadata: dict[str, object] = {"failure_reason": "fx_unavailable"}
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.record_auto_pricing_failure_if_current(
            row.id,
            expected_cost_minor=1000,
            expected_cost_currency="EUR",
            expected_price_minor=1210,
            expected_selling_currency="USD",
            expected_pricing_metadata=expected_metadata,
            pricing_metadata=failure_metadata,
            preserve_valid_price=True,
        )

        assert result is not None
        assert result.selling_price_minor == 1210
        assert result.selling_currency == "USD"
        assert result.pricing_metadata == failure_metadata
        assert session.commits == 1

    async def test_clears_stale_price(self) -> None:
        row = await _priced_row_async()
        expected_metadata = dict(row.pricing_metadata)
        failure_metadata: dict[str, object] = {"failure_reason": "fx_unavailable"}
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.record_auto_pricing_failure_if_current(
            row.id,
            expected_cost_minor=1000,
            expected_cost_currency="EUR",
            expected_price_minor=1210,
            expected_selling_currency="USD",
            expected_pricing_metadata=expected_metadata,
            pricing_metadata=failure_metadata,
            preserve_valid_price=False,
        )

        assert result is not None
        assert result.selling_price_minor == 0
        assert result.selling_currency == "USD"
        assert result.pricing_metadata == failure_metadata
        assert session.commits == 1

    async def test_returns_none_on_metadata_mismatch(self) -> None:
        row = await _priced_row_async()
        session = _ScriptedSession([_Result(row)])
        repo = _repo(session)

        result = await repo.record_auto_pricing_failure_if_current(
            row.id,
            expected_cost_minor=1000,
            expected_cost_currency="EUR",
            expected_price_minor=1210,
            expected_selling_currency="USD",
            expected_pricing_metadata={"stale": "snapshot"},
            pricing_metadata={"failure_reason": "fx_unavailable"},
            preserve_valid_price=True,
        )

        assert result is None
        assert row.selling_price_minor == 1210
        assert session.commits == 0


class TestGetByRef:
    async def test_returns_row_for_account_scoped_ref(self) -> None:
        row = _row(provider_account_id="fra-account")
        session = _ScriptedSession([_Result([row])])
        repo = _repo(session)

        result = await repo.get_by_ref(
            "leaseweb", "plan-a", "eu-west", provider_account_id="fra-account"
        )

        assert result is not None
        assert result.product_id == "plan-a"
        assert result.provider_account_id == "fra-account"
        assert session.commits == 0

    async def test_returns_none_when_missing(self) -> None:
        session = _ScriptedSession([_Result([])])
        repo = _repo(session)

        result = await repo.get_by_ref(
            "leaseweb", "plan-a", "eu-west", provider_account_id="fra-account"
        )

        assert result is None

    async def test_raises_when_ref_is_ambiguous(self) -> None:
        session = _ScriptedSession([_Result([_row(), _row()])])
        repo = _repo(session)

        try:
            await repo.get_by_ref(
                "leaseweb", "plan-a", "eu-west", provider_account_id="fra-account"
            )
        except ValueError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("ambiguous ref must raise")
