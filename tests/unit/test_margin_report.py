"""Tests for the provider-vs-customer margin report (M06-008).

Acceptance: daily expected margin and anomalies must be visible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.billing.service import MarginReportService

DAY = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
S1 = uuid4()
S2 = uuid4()


def _period(
    server_id: UUID,
    start: datetime,
    quanta: int,
    cost: int,
    selling: int,
    currency: str = "EUR",
) -> object:
    from cloud_platform.modules.billing.service import AccrualPeriod

    return AccrualPeriod(
        server_id=server_id,
        wallet_id=uuid4(),
        period_start=start,
        period_end=start + timedelta(hours=quanta),
        quanta=quanta,
        cost_minor=cost,
        selling_minor=selling,
        currency=currency,
        idempotency_key=f"k-{server_id}-{start}",
    )


class _Repo:
    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.calls: list[tuple[datetime, datetime]] = []

    async def list_between(self, start, end):
        self.calls.append((start, end))
        return [r for r in self.rows if start <= r.period_start < end]


def _service(rows: list) -> tuple[MarginReportService, _Repo]:
    repo = _Repo(rows)
    return MarginReportService(repo), repo


class TestTotals:
    async def test_empty_window(self) -> None:
        svc, _ = _service([])
        report = await svc.report(DAY, DAY + timedelta(days=1))

        assert report.periods == 0
        assert report.quanta == 0
        assert report.revenues == {}
        assert report.is_clean
        assert report.margin("EUR") == 0

    async def test_daily_totals_and_per_server(self) -> None:
        rows = [
            _period(S1, DAY + timedelta(hours=1), quanta=1, cost=700, selling=1000),
            _period(S1, DAY + timedelta(hours=5), quanta=2, cost=1400, selling=2000),
            _period(S2, DAY + timedelta(hours=2), quanta=1, cost=900, selling=1300),
        ]
        svc, _ = _service(rows)
        report = await svc.report_for_day(DAY)

        assert report.periods == 3
        assert report.quanta == 4
        assert report.revenues["EUR"] == 4300
        assert report.costs["EUR"] == 3000
        assert report.margin("EUR") == 1300

        m1 = report.per_server[S1]
        assert (m1.quanta, m1.revenue_minor, m1.cost_minor, m1.margin_minor) == (3, 3000, 2100, 900)
        m2 = report.per_server[S2]
        assert (m2.quanta, m2.revenue_minor, m2.cost_minor, m2.margin_minor) == (1, 1300, 900, 400)
        assert report.is_clean

    async def test_window_boundaries(self) -> None:
        inside = _period(S1, DAY, quanta=1, cost=1, selling=2)
        at_end = _period(S2, DAY + timedelta(days=1), quanta=1, cost=1, selling=2)
        svc, _ = _service([inside, at_end])

        report = await svc.report(DAY, DAY + timedelta(days=1))

        # [start, end): the period at start is in, the one at end is out
        assert report.periods == 1
        assert report.per_server and S1 in report.per_server

    async def test_requires_valid_window(self) -> None:
        svc, _ = _service([])
        with pytest.raises(ValueError):
            await svc.report(DAY, DAY)
        with pytest.raises(ValueError):
            await svc.report(DAY + timedelta(hours=1), DAY)

    async def test_report_for_day_uses_utc_day(self) -> None:
        svc, repo = _service([_period(S1, DAY, quanta=1, cost=1, selling=2)])
        await svc.report_for_day(DAY)

        assert repo.calls == [(DAY, DAY + timedelta(days=1))]


class TestAnomalies:
    async def test_negative_margin_flagged(self) -> None:
        rows = [_period(S1, DAY + timedelta(hours=1), quanta=1, cost=1100, selling=1000)]
        svc, _ = _service(rows)
        report = await svc.report(DAY, DAY + timedelta(days=1))

        assert len(report.anomalies) == 1
        a = report.anomalies[0]
        assert a.server_id == S1
        assert a.reason.startswith("NEGATIVE_MARGIN")
        assert "cost 1100 >= revenue 1000" in a.reason
        assert not report.is_clean
        # the totals still include the period - visible, not hidden
        assert report.margin("EUR") == -100

    async def test_zero_cost_settled_flagged_unknown(self) -> None:
        rows = [_period(S1, DAY + timedelta(hours=1), quanta=1, cost=0, selling=1000)]
        svc, _ = _service(rows)
        report = await svc.report(DAY, DAY + timedelta(days=1))

        assert len(report.anomalies) == 1
        assert report.anomalies[0].reason.startswith("COST_UNKNOWN")

    async def test_equal_cost_revenue_is_negative_margin(self) -> None:
        rows = [_period(S1, DAY, quanta=1, cost=1000, selling=1000)]
        svc, _ = _service(rows)
        report = await svc.report(DAY, DAY + timedelta(days=1))

        assert report.anomalies[0].reason.startswith("NEGATIVE_MARGIN")
        assert report.margin("EUR") == 0

    async def test_mixed_currency_flagged(self) -> None:
        rows = [
            _period(S1, DAY, quanta=1, cost=100, selling=200, currency="EUR"),
            _period(S2, DAY, quanta=1, cost=100, selling=200, currency="USD"),
        ]
        svc, _ = _service(rows)
        report = await svc.report(DAY, DAY + timedelta(days=1))

        assert report.revenues == {"EUR": 200, "USD": 200}
        mixed = [a for a in report.anomalies if a.reason.startswith("MIXED_CURRENCY")]
        assert len(mixed) == 1
        assert "EUR" in mixed[0].reason and "USD" in mixed[0].reason
        assert report.margin("EUR") == 100  # per-currency totals stay usable

    async def test_anomaly_sorting_deterministic(self) -> None:
        rows = [
            _period(S2, DAY + timedelta(hours=3), quanta=1, cost=0, selling=100),
            _period(S1, DAY + timedelta(hours=1), quanta=1, cost=200, selling=100),
            _period(S1, DAY + timedelta(hours=2), quanta=1, cost=0, selling=100),
        ]
        svc, _ = _service(rows)
        first = await svc.report(DAY, DAY + timedelta(days=1))
        second = await svc.report(DAY, DAY + timedelta(days=1))
        assert [a.period_start for a in first.anomalies] == [
            (DAY + timedelta(hours=h)) for h in (1, 2, 3)
        ]
        assert first.anomalies == second.anomalies  # repeatable


class TestRender:
    async def test_render_is_ascii_and_complete(self) -> None:
        rows = [
            _period(S1, DAY + timedelta(hours=1), quanta=1, cost=700, selling=1000),
            _period(S2, DAY + timedelta(hours=2), quanta=1, cost=900, selling=1300),
        ]
        svc, _ = _service(rows)
        report = await svc.report(DAY, DAY + timedelta(days=1))
        text = report.render()

        text.encode("ascii")  # raises if non-ASCII leaked in
        assert "periods=2" in text
        assert "quanta=2" in text
        assert "EUR: revenue=2300 cost=1600 margin=700" in text
        assert f"server {S1}:" in text
        assert f"server {S2}:" in text
        assert "anomalies: none" in text

    async def test_render_lists_anomalies(self) -> None:
        rows = [_period(S1, DAY, quanta=1, cost=5000, selling=1000)]
        svc, _ = _service(rows)
        report = await svc.report(DAY, DAY + timedelta(days=1))
        text = report.render()

        assert "anomalies (1):" in text
        assert "NEGATIVE_MARGIN" in text
        assert f"server {S1}" in text
