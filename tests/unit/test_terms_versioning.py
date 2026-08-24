"""Tests for terms acceptance versioning (M02-004).

Acceptance: provisioning can require the latest terms.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    TermsAcceptanceRequiredError,
    TermsVersion,
    User,
    UserStatus,
)
from cloud_platform.modules.users.terms import TermsService

NOW = datetime(2026, 8, 24, tzinfo=UTC)
USER_ID = uuid4()


def _admin() -> User:
    return User(id=uuid4(), username="root", email="r@example.com", role=Role.ADMIN)


def _user(terms_version: int | None = None) -> User:
    return User(
        id=USER_ID,
        username="alice",
        email="a@example.com",
        role=Role.USER,
        status=UserStatus.ACTIVE,
        terms_version=terms_version,
    )


def _terms(version: int = 1) -> TermsVersion:
    return TermsVersion(version=version, body=f"terms {version}", effective_at=NOW)


class FakeTermsRepo:
    def __init__(self, versions: list[TermsVersion] | None = None) -> None:
        self.versions = {t.version: t for t in (versions or [])}
        self.published: list[TermsVersion] = []

    async def get(self, version: int) -> TermsVersion | None:
        return self.versions.get(version)

    async def get_latest(self) -> TermsVersion | None:
        if not self.versions:
            return None
        return max(self.versions.values(), key=lambda t: t.version)

    async def list_all(self) -> list[TermsVersion]:
        return [self.versions[k] for k in sorted(self.versions)]

    async def publish(self, terms: TermsVersion) -> TermsVersion:
        if terms.version in self.versions:
            raise ValueError("exists")
        self.versions[terms.version] = terms
        self.published.append(terms)
        return terms


class FakeUserRepo:
    def __init__(self, user: User | None) -> None:
        self.user = user
        self.update_calls: list[tuple] = []

    async def get(self, user_id) -> User | None:
        return self.user if self.user is not None and self.user.id == user_id else None

    async def update_terms(self, user_id, terms_version: int) -> User:
        assert self.user is not None
        self.update_calls.append((user_id, terms_version))
        self.user.accept_terms(terms_version)
        return self.user


def _service(terms_repo: FakeTermsRepo, user_repo: FakeUserRepo) -> tuple[TermsService, AsyncMock]:
    audit = AsyncMock()
    return TermsService(terms_repo, user_repo, audit), audit  # type: ignore[return-value]


class TestDomainGate:
    @pytest.mark.parametrize(
        ("user_version", "latest", "needs"),
        [
            (None, None, False),  # nothing published
            (1, None, False),
            (None, 2, True),  # never accepted
            (1, 2, True),  # stale
            (2, 2, False),  # current
            (3, 2, False),  # future (clock/data anomaly) does not block
        ],
    )
    def test_needs_terms_acceptance(self, user_version, latest, needs) -> None:
        assert _user(user_version).needs_terms_acceptance(latest) is needs


class TestPublish:
    async def test_first_version_is_one(self) -> None:
        repo, user_repo = FakeTermsRepo(), FakeUserRepo(None)
        service, audit = _service(repo, user_repo)
        published = await service.publish(admin=_admin(), summary="first", body=" be careful ")
        assert published.version == 1
        assert published.body == "be careful"
        event = audit.append.call_args.args[0]
        assert event.action == "terms.publish"
        assert event.resource_id == "1"

    async def test_versions_increment(self) -> None:
        repo = FakeTermsRepo([_terms(1), _terms(2)])
        service, _ = _service(repo, FakeUserRepo(None))
        published = await service.publish(admin=_admin(), summary="s", body="b")
        assert published.version == 3
        assert (await repo.get_latest()) is not None
        assert (await repo.get_latest()).version == 3

    async def test_non_admin_cannot_publish(self) -> None:
        repo, user_repo = FakeTermsRepo(), FakeUserRepo(None)
        service, audit = _service(repo, user_repo)
        with pytest.raises(PermissionDeniedError):
            await service.publish(admin=_user(), summary="s", body="b")
        audit.append.assert_not_awaited()
        assert repo.published == []


class TestAccept:
    async def test_accept_records_current_version(self) -> None:
        repo = FakeTermsRepo([_terms(1), _terms(2)])
        user = _user(1)
        user_repo = FakeUserRepo(user)
        service, audit = _service(repo, user_repo)

        updated = await service.accept(user_id=USER_ID)

        assert updated.terms_version == 2
        assert user_repo.update_calls == [(USER_ID, 2)]
        event = audit.append.call_args.args[0]
        assert event.action == "terms.accept"

    async def test_accept_idempotent_when_current(self) -> None:
        repo = FakeTermsRepo([_terms(2)])
        user = _user(2)
        user_repo = FakeUserRepo(user)
        service, audit = _service(repo, user_repo)
        updated = await service.accept(user_id=USER_ID)
        assert updated.terms_version == 2
        assert user_repo.update_calls == []
        audit.append.assert_not_awaited()

    async def test_accept_noop_without_published_terms(self) -> None:
        user = _user(None)
        user_repo = FakeUserRepo(user)
        service, _ = _service(FakeTermsRepo(), user_repo)
        updated = await service.accept(user_id=USER_ID)
        assert updated.terms_version is None
        assert user_repo.update_calls == []

    async def test_accept_unknown_user(self) -> None:
        service, _ = _service(FakeTermsRepo([_terms(1)]), FakeUserRepo(None))
        with pytest.raises(LookupError):
            await service.accept(user_id=uuid4())


class TestRequireLatest:
    async def test_raises_for_never_accepted(self) -> None:
        service, _ = _service(FakeTermsRepo([_terms(2)]), FakeUserRepo(None))
        with pytest.raises(TermsAcceptanceRequiredError, match="never"):
            await service.require_latest(_user(None))

    async def test_raises_for_stale(self) -> None:
        service, _ = _service(FakeTermsRepo([_terms(2)]), FakeUserRepo(None))
        with pytest.raises(TermsAcceptanceRequiredError, match="accepted terms 1"):
            await service.require_latest(_user(1))

    async def test_passes_when_current(self) -> None:
        service, _ = _service(FakeTermsRepo([_terms(2)]), FakeUserRepo(None))
        await service.require_latest(_user(2))  # no raise

    async def test_passes_when_nothing_published(self) -> None:
        service, _ = _service(FakeTermsRepo(), FakeUserRepo(None))
        await service.require_latest(_user(None))  # no raise


class TestProvisioningGate:
    """The create-server command must honor the terms gate (step 1.5)."""

    def _service_with_gate(self, gate: AsyncMock) -> tuple[object, AsyncMock]:
        from cloud_platform.modules.compute.service import CreateServerService

        catalog = AsyncMock()
        service = CreateServerService(
            server_repo=AsyncMock(),  # type: ignore[arg-type]
            account_repo=AsyncMock(),  # type: ignore[arg-type]
            catalog_repo=catalog,  # type: ignore[arg-type]
            price_book_service=AsyncMock(),  # type: ignore[arg-type]
            snapshot_service=AsyncMock(),  # type: ignore[arg-type]
            wallet_repo=AsyncMock(),  # type: ignore[arg-type]
            hold_repo=AsyncMock(),  # type: ignore[arg-type]
            audit_repo=AsyncMock(),  # type: ignore[arg-type]
            book_name="retail-eur",
            terms=gate,  # type: ignore[arg-type]
        )
        return service, catalog

    async def test_stale_terms_block_before_catalog(self) -> None:
        gate = AsyncMock()
        gate.require_latest.side_effect = TermsAcceptanceRequiredError("stale")
        service, catalog = self._service_with_gate(gate)

        with pytest.raises(TermsAcceptanceRequiredError):
            await service.create_server(  # type: ignore[union-attr]
                user=_user(1),
                offer_ref=None,  # type: ignore[arg-type]
                idempotency_key="k",
            )

        gate.require_latest.assert_awaited_once()
        catalog.get_offer.assert_not_awaited()  # blocked before any catalog work

    async def test_current_terms_proceed(self) -> None:
        from cloud_platform.modules.catalog.domain import OfferRef

        gate = AsyncMock()
        service, catalog = self._service_with_gate(gate)

        try:
            await service.create_server(  # type: ignore[union-attr]
                user=_user(2),
                offer_ref=OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1"),
                idempotency_key="k",
            )
        except TermsAcceptanceRequiredError:
            pytest.fail("current terms must not block provisioning")
        except Exception:
            pass  # mock-driven failure after the gate is fine for this test

        gate.require_latest.assert_awaited_once()
        catalog.get_offer.assert_awaited()  # the flow got past the gate

    async def test_no_gate_keeps_legacy_behavior(self) -> None:
        from cloud_platform.modules.catalog.domain import OfferRef
        from cloud_platform.modules.compute.service import CreateServerService

        catalog = AsyncMock()
        service = CreateServerService(
            server_repo=AsyncMock(),  # type: ignore[arg-type]
            account_repo=AsyncMock(),  # type: ignore[arg-type]
            catalog_repo=catalog,  # type: ignore[arg-type]
            price_book_service=AsyncMock(),  # type: ignore[arg-type]
            snapshot_service=AsyncMock(),  # type: ignore[arg-type]
            wallet_repo=AsyncMock(),  # type: ignore[arg-type]
            hold_repo=AsyncMock(),  # type: ignore[arg-type]
            audit_repo=AsyncMock(),  # type: ignore[arg-type]
            book_name="retail-eur",
        )

        try:
            await service.create_server(  # type: ignore[union-attr]
                user=_user(None),
                offer_ref=OfferRef(provider_key="hetzner", plan_id="cx22", location_id="fsn1"),
                idempotency_key="k",
            )
        except TermsAcceptanceRequiredError:
            pytest.fail("without a wired gate, terms never block")
        except Exception:
            pass  # mock-driven failure after the gate is fine for this test

        catalog.get_offer.assert_awaited()
