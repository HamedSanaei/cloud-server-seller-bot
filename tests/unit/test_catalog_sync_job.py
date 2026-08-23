"""Tests for the catalog sync job with lock (M04-006).

Acceptance: concurrent syncs cannot corrupt catalog.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from cloud_platform.modules.catalog.domain import (
    CatalogSyncJob,
    CatalogSyncStep,
    CatalogSyncStepReport,
)
from cloud_platform.modules.catalog.repository import (
    _CATALOG_SYNC_LOCK_KEY,
    PostgresAdvisoryCatalogSyncLock,
)
from cloud_platform.providers.hetzner.sync import (
    SyncResult,
    build_catalog_sync_job,
)


class ManualLock:
    """A lock whose held state is controlled by the test."""

    def __init__(self, held: bool = False) -> None:
        self.held = held
        self.acquire_calls = 0
        self.release_calls = 0

    @asynccontextmanager
    async def guard(self):
        self.acquire_calls += 1
        if self.held:
            yield False
            return
        self.held = True
        try:
            yield True
        finally:
            self.held = False
            self.release_calls += 1


def _step(
    name: str,
    result: CatalogSyncStepReport | None = None,
    fail: Exception | None = None,
) -> CatalogSyncStep:
    async def run() -> CatalogSyncStepReport:
        if fail is not None:
            raise fail
        return result or CatalogSyncStepReport(name=name, fetched=1, upserted=1)

    return CatalogSyncStep(name=name, run=run)


class TestCatalogSyncJob:
    async def test_runs_all_steps_in_order_and_releases_lock(self) -> None:
        lock = ManualLock()
        order: list[str] = []

        def recording(name: str) -> CatalogSyncStep:
            async def run() -> CatalogSyncStepReport:
                order.append(name)
                return CatalogSyncStepReport(name=name, fetched=1, upserted=1)

            return CatalogSyncStep(name=name, run=run)

        job = CatalogSyncJob(lock, (recording("a"), recording("b"), recording("c")))
        report = await job.run()

        assert report.ran and not report.skipped
        assert order == ["a", "b", "c"]
        assert [r.name for r in report.steps] == ["a", "b", "c"]
        assert lock.held is False
        assert lock.release_calls == 1

    async def test_lock_held_skips_entire_run(self) -> None:
        lock = ManualLock(held=True)
        touched: list[str] = []

        def touching(name: str) -> CatalogSyncStep:
            async def run() -> CatalogSyncStepReport:
                touched.append(name)
                return CatalogSyncStepReport(name=name)

            return CatalogSyncStep(name=name, run=run)

        job = CatalogSyncJob(lock, (touching("a"), touching("b")))
        report = await job.run()

        assert report.skipped and not report.ran
        assert report.steps == ()
        assert "held by another sync" in (report.reason or "")
        assert touched == []  # nothing was written by the losing run

    async def test_failing_step_is_recorded_and_run_continues(self) -> None:
        lock = ManualLock()
        job = CatalogSyncJob(
            lock,
            (
                _step("a", fail=RuntimeError("provider 500")),
                _step("b", result=CatalogSyncStepReport(name="b", fetched=2, upserted=2)),
            ),
        )
        report = await job.run()

        assert report.ran
        assert report.steps[0].name == "a"
        assert report.steps[0].error == "provider 500"
        assert report.steps[1].name == "b"
        assert report.steps[1].upserted == 2
        assert lock.held is False

    async def test_lock_released_when_every_step_fails(self) -> None:
        lock = ManualLock()
        job = CatalogSyncJob(
            lock,
            (_step("a", fail=RuntimeError("x")), _step("b", fail=RuntimeError("y"))),
        )
        report = await job.run()

        assert report.ran
        assert all(s.error for s in report.steps)
        assert lock.held is False
        assert lock.release_calls == 1

    async def test_empty_steps_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one step"):
            CatalogSyncJob(ManualLock(), ())


class _LockResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class _FakeSession:
    def __init__(self, acquired: bool) -> None:
        self.acquired = acquired
        self.statements: list[str] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        self.statements.append(str(stmt))
        assert params == {"key": _CATALOG_SYNC_LOCK_KEY}
        return _LockResult(self.acquired)


def _one_shot(session: Any) -> Any:
    """A session factory that always yields the given session (pool=1)."""

    @asynccontextmanager
    async def factory():
        yield session

    return factory()


class TestPostgresAdvisoryLock:
    def _make(self, acquired: bool) -> tuple[PostgresAdvisoryCatalogSyncLock, _FakeSession]:
        session = _FakeSession(acquired)
        lock = PostgresAdvisoryCatalogSyncLock(lambda: _one_shot(session))  # type: ignore[arg-type]
        return lock, session

    async def test_acquire_true_yields_true_and_unlocks_same_session(self) -> None:
        lock, session = self._make(True)
        async with lock.guard() as acquired:
            assert acquired is True
        assert session.statements == [
            "SELECT pg_try_advisory_lock(:key)",
            "SELECT pg_advisory_unlock(:key)",
        ]

    async def test_acquire_false_never_unlocks(self) -> None:
        lock, session = self._make(False)
        async with lock.guard() as acquired:
            assert acquired is False
        assert session.statements == ["SELECT pg_try_advisory_lock(:key)"]

    async def test_unlock_runs_even_if_guarded_code_raises(self) -> None:
        lock, session = self._make(True)
        with pytest.raises(RuntimeError, match="boom"):
            async with lock.guard() as acquired:
                assert acquired
                raise RuntimeError("boom")
        assert session.statements == [
            "SELECT pg_try_advisory_lock(:key)",
            "SELECT pg_advisory_unlock(:key)",
        ]


class AdvisoryStyleLock:
    """Models a pg advisory lock: one owner at a time, holder wins, others skip."""

    def __init__(self) -> None:
        self._owner: asyncio.Event | None = None

    @asynccontextmanager
    async def guard(self):
        if self._owner is not None:
            yield False
            return
        self._owner = asyncio.Event()
        try:
            yield True
        finally:
            self._owner = None


class TestConcurrentSyncs:
    async def test_concurrent_syncs_cannot_corrupt_catalog(self) -> None:
        """The acceptance: two syncs racing -> exactly one runs, catalog intact.

        Each step is a slow read-modify-write; without the lock the two runs
        would interleave and each catalog entry could be written twice (or a
        partially-updated view could be observed). With the lock the loser
        skips the whole run, so every step executes exactly once.
        """
        catalog: dict[str, list[str]] = {}
        calls = {"locations": 0, "plans": 0}
        lock = AdvisoryStyleLock()

        def make_job() -> CatalogSyncJob:
            def step(name: str) -> CatalogSyncStep:
                async def run() -> CatalogSyncStepReport:
                    calls[name] += 1
                    await asyncio.sleep(0.01)  # widen the race window
                    existing = catalog.get(name, [])
                    catalog[name] = [*existing, "synced"]  # read-modify-write
                    return CatalogSyncStepReport(name=name, fetched=1, upserted=1)

                return CatalogSyncStep(name=name, run=run)

            return CatalogSyncJob(lock, (step("locations"), step("plans")))

        results = await asyncio.gather(make_job().run(), make_job().run())

        ran = [r for r in results if r.ran]
        skipped = [r for r in results if r.skipped]
        assert len(ran) == 1
        assert len(skipped) == 1
        # Every step ran exactly once in total - the catalog was written by
        # exactly one sync, so it holds a consistent, complete state.
        assert calls == {"locations": 1, "plans": 1}
        assert catalog == {"locations": ["synced"], "plans": ["synced"]}


class FakeSyncer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def sync_locations(self) -> SyncResult:
        self.calls.append("locations")
        return SyncResult(
            total_fetched=2, total_upserted=1, total_skipped=1, errors=["page 2: 500"]
        )

    async def sync_plans(self) -> SyncResult:
        self.calls.append("plans")
        return SyncResult(total_fetched=3, total_upserted=3, total_skipped=0, errors=[])

    async def sync_images(self) -> SyncResult:
        self.calls.append("images")
        return SyncResult(total_fetched=4, total_upserted=4, total_skipped=0, errors=[])


class TestBuilder:
    async def test_build_catalog_sync_job_wires_all_three_steps(self) -> None:
        syncer = FakeSyncer()
        lock = ManualLock()
        job = build_catalog_sync_job(syncer, lock)  # type: ignore[arg-type]

        report = await job.run()

        assert syncer.calls == ["locations", "plans", "images"]
        assert report.ran
        assert [(r.name, r.fetched, r.upserted, r.errors) for r in report.steps] == [
            ("locations", 2, 1, ("page 2: 500",)),
            ("plans", 3, 3, ()),
            ("images", 4, 4, ()),
        ]

    async def test_builder_reuses_lock_across_steps(self) -> None:
        syncer = FakeSyncer()
        lock = ManualLock()
        job = build_catalog_sync_job(syncer, lock)  # type: ignore[arg-type]

        await job.run()

        assert lock.acquire_calls == 1
        assert lock.release_calls == 1
