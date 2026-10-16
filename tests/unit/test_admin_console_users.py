"""Authorization, bounds and delegation for the admin-console user facade."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cloud_platform.modules.admin_console import (
    AdminServerSummary,
    AdminUserProfile,
    AdminUserSearchPage,
    AdminUserService,
)
from cloud_platform.modules.admin_console.users import (
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_PAGE_SIZE,
    MAX_QUERY_LENGTH,
    MAX_REASON_LENGTH,
    SERVER_PAGE_LIMIT,
)
from cloud_platform.modules.compute.domain import CloudServer, ServerLifecycleState
from cloud_platform.modules.users.domain import (
    PermissionDeniedError,
    Role,
    User,
    UserNotFound,
    UserStatus,
)
from cloud_platform.modules.wallet.domain import Wallet

CONFIGURED = (77110022,)
ADMIN_TID = 77110022
SECOND_ADMIN_TID = 77110033
TARGET_TID = 224466

_PROVIDER_SERVER_ID = "srv-secret-9001"
_CREDENTIAL_ACCOUNT_ID = "cred-secret-42"
_OFFER_FINGERPRINT_TOKEN = "offer-secret-token"
_IDEMPOTENCY_KEY = "idem-secret-key"
_IPV4 = "203.0.113.7"
_PROVIDER_ACCOUNT_ID = uuid4()


def _admin(
    *,
    role: Role = Role.ADMIN,
    status: UserStatus = UserStatus.ACTIVE,
    tid: int = ADMIN_TID,
    uid: UUID | None = None,
) -> User:
    return User(
        id=uid or uuid4(),
        username="boss",
        email="boss@ex.com",
        role=role,
        status=status,
        telegram_user_id=tid,
    )


def _user(
    *,
    telegram_user_id: int | None = None,
    username: str = "member",
    role: Role = Role.USER,
    status: UserStatus = UserStatus.ACTIVE,
    uid: UUID | None = None,
) -> User:
    return User(
        id=uid or uuid4(),
        username=username,
        email=f"{username}@ex.com",
        role=role,
        status=status,
        telegram_user_id=telegram_user_id,
    )


def _server(
    *, owner: UUID, state: ServerLifecycleState = ServerLifecycleState.RUNNING
) -> CloudServer:
    return CloudServer(
        id=uuid4(),
        user_id=owner,
        provider_key="leaseweb",
        provider_account_id=_PROVIDER_ACCOUNT_ID,
        state=state,
        provider_server_id=_PROVIDER_SERVER_ID,
        ipv4=_IPV4,
        idempotency_key=_IDEMPOTENCY_KEY,
        credential_account_id=_CREDENTIAL_ACCOUNT_ID,
        offer_fingerprint={"token": _OFFER_FINGERPRINT_TOKEN},
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )


class FakeUsers:
    """In-memory user port recording every interaction."""

    def __init__(
        self,
        *,
        fresh: User | None = None,
        targets: dict[int, User] | None = None,
        search_result: list[User] | None = None,
    ) -> None:
        self._fresh = fresh
        self._targets = targets or {}
        self._search_result = list(search_result or [])
        self.get_calls: list[UUID] = []
        self.search_calls: list[tuple[str, int, int]] = []
        self.telegram_lookups: list[int] = []
        self.change_status_calls: list[dict[str, object]] = []
        self.create_calls = 0
        self.receipts: dict[tuple[UUID, str], UserStatus] = {}

    async def get(self, user_id: UUID) -> User | None:
        self.get_calls.append(user_id)
        return self._fresh

    async def get_by_telegram_user_id(self, telegram_user_id: int) -> User | None:
        self.telegram_lookups.append(telegram_user_id)
        return self._targets.get(telegram_user_id)

    async def search(self, query: str, *, offset: int = 0, limit: int = 20) -> list[User]:
        self.search_calls.append((query, offset, limit))
        return list(self._search_result)

    async def change_status_idempotently(
        self,
        user_id: UUID,
        status: UserStatus,
        *,
        actor_id: UUID,
        idempotency_key: str,
        reason: str,
        admin_configured_ids: tuple[int, ...] | None = None,
    ) -> UserStatus:
        self.change_status_calls.append(
            {
                "user_id": user_id,
                "status": status,
                "actor_id": actor_id,
                "idempotency_key": idempotency_key,
                "reason": reason,
                "admin_configured_ids": admin_configured_ids,
            }
        )
        key = (actor_id, idempotency_key)
        if key in self.receipts:
            return self.receipts[key]
        self.receipts[key] = status
        return status

    async def create(self, *args: object, **kwargs: object) -> User:
        self.create_calls += 1
        raise AssertionError("admin console must not create accounts")


class FakeWallets:
    """Wallet port: reads only, mutations fail closed."""

    def __init__(self, wallet: Wallet | None = None) -> None:
        self._wallet = wallet
        self.get_calls: list[UUID] = []
        self.get_or_create_calls = 0
        self.adjust_calls = 0

    async def get(self, user_id: UUID) -> Wallet | None:
        self.get_calls.append(user_id)
        return self._wallet

    async def get_or_create(self, *args: object, **kwargs: object) -> Wallet:
        self.get_or_create_calls += 1
        raise AssertionError("admin console must not create wallets")

    async def adjust(self, *args: object, **kwargs: object) -> Wallet:
        self.adjust_calls += 1
        raise AssertionError("admin console must not adjust wallets")


class FakeServers:
    """Server port: paged reads only, writes fail closed."""

    def __init__(self, *, servers: list[CloudServer] | None = None, total: int = 0) -> None:
        self._servers = list(servers or [])
        self._total = total
        self.list_calls: list[tuple[UUID, int, int]] = []
        self.save_calls = 0
        self.create_calls = 0

    async def list_by_user_paged(
        self,
        user_id: UUID,
        *,
        offset: int = 0,
        limit: int = 20,
        hide_failed_before: datetime | None = None,
    ) -> tuple[list[CloudServer], int]:
        self.list_calls.append((user_id, offset, limit))
        return list(self._servers), self._total

    async def save(self, *args: object, **kwargs: object) -> CloudServer:
        self.save_calls += 1
        raise AssertionError("admin console must not save servers")

    async def create(self, *args: object, **kwargs: object) -> CloudServer:
        self.create_calls += 1
        raise AssertionError("admin console must not create servers")


def _service(
    users: FakeUsers,
    wallets: FakeWallets | None = None,
    servers: FakeServers | None = None,
) -> AdminUserService:
    return AdminUserService(
        users, wallets or FakeWallets(), servers or FakeServers(), configured_ids=CONFIGURED
    )


async def _invoke(service: AdminUserService, actor: object, method: str) -> object:
    if method == "search":
        return await service.search(actor, "needle")
    if method == "profile":
        return await service.profile(actor, TARGET_TID)
    return await service.set_status(
        actor, TARGET_TID, UserStatus.FROZEN, reason="review", idempotency_key="auth-key"
    )


def _auth_case(
    label: str,
) -> tuple[object, FakeUsers, tuple[int, ...], bool]:
    if label == "role_user":
        actor = _admin(role=Role.USER)
        return actor, FakeUsers(fresh=actor), CONFIGURED, False
    if label == "unconfigured":
        actor = _admin(tid=555001)
        return actor, FakeUsers(fresh=actor), CONFIGURED, False
    if label == "frozen":
        actor = _admin(status=UserStatus.FROZEN)
        return actor, FakeUsers(fresh=actor), CONFIGURED, False
    if label == "banned":
        actor = _admin(status=UserStatus.BANNED)
        return actor, FakeUsers(fresh=actor), CONFIGURED, False
    if label == "forged":
        actor = SimpleNamespace(
            id=uuid4(),
            username="notif",
            email="notif@ex.com",
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
            telegram_user_id=ADMIN_TID,
        )
        return actor, FakeUsers(fresh=None), CONFIGURED, False
    if label == "fresh_none":
        actor = _admin()
        return actor, FakeUsers(fresh=None), CONFIGURED, True
    if label == "fresh_revoked":
        actor = _admin()
        return actor, FakeUsers(fresh=replace(actor, role=Role.USER)), CONFIGURED, True
    if label == "fresh_identity":
        actor = _admin()
        fresh = _admin(uid=actor.id, tid=SECOND_ADMIN_TID)
        return actor, FakeUsers(fresh=fresh), (ADMIN_TID, SECOND_ADMIN_TID), True
    raise AssertionError(f"unknown case {label!r}")


async def test_search_returns_items_and_forwards_trimmed_query() -> None:
    actor = _admin()
    results = [
        _user(username="alice", telegram_user_id=1001),
        _user(username="bob", telegram_user_id=1002),
    ]
    users = FakeUsers(fresh=actor, search_result=results)
    page = await _service(users).search(actor, "  alice  ", offset=5, limit=10)
    assert isinstance(page, AdminUserSearchPage)
    assert page.items == tuple(results)
    assert page.offset == 5
    assert page.limit == 10
    assert users.search_calls == [("alice", 5, 10)]


async def test_search_caps_limit_to_max_page_size() -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor)
    page = await _service(users).search(actor, "q", offset=0, limit=1000)
    assert users.search_calls == [("q", 0, MAX_PAGE_SIZE)]
    assert page.limit == MAX_PAGE_SIZE


@pytest.mark.parametrize("offset", [-1, 1.5, "0", None, True])
async def test_search_rejects_invalid_offset(offset: object) -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor)
    with pytest.raises(ValueError):
        await _service(users).search(actor, "q", offset=offset)
    assert users.search_calls == []


@pytest.mark.parametrize("limit", [0, -1, True])
async def test_search_rejects_invalid_limit(limit: object) -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor)
    with pytest.raises(ValueError):
        await _service(users).search(actor, "q", limit=limit)
    assert users.search_calls == []


async def test_search_trims_and_truncates_query() -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor)
    service = _service(users)
    await service.search(actor, "  spaced  ")
    assert users.search_calls[-1] == ("spaced", 0, 20)
    await service.search(actor, "q" * 250)
    assert users.search_calls[-1] == ("q" * MAX_QUERY_LENGTH, 0, 20)


@pytest.mark.parametrize("query", [None, 5])
async def test_search_rejects_non_string_query(query: object) -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor)
    with pytest.raises(ValueError):
        await _service(users).search(actor, query)
    assert users.search_calls == []


async def test_search_numeric_query_uses_exact_telegram_id_lookup() -> None:
    actor = _admin()
    target = _user(username="member", telegram_user_id=12345)
    decoy = _user(username="12345", telegram_user_id=999)
    users = FakeUsers(fresh=actor, targets={12345: target}, search_result=[decoy])
    page = await _service(users).search(actor, "12345")
    assert page.items == (target,)
    assert page.items[0] is target
    assert users.telegram_lookups == [12345]
    assert users.search_calls == []


async def test_search_numeric_query_does_not_match_numeric_username() -> None:
    actor = _admin()
    decoy = _user(username="12345", telegram_user_id=999)
    users = FakeUsers(fresh=actor, targets={}, search_result=[decoy])
    page = await _service(users).search(actor, "12345")
    assert page.items == ()
    assert users.telegram_lookups == [12345]
    assert users.search_calls == []


async def test_search_numeric_query_trims_surrounding_whitespace() -> None:
    actor = _admin()
    target = _user(telegram_user_id=54321)
    users = FakeUsers(fresh=actor, targets={54321: target})
    page = await _service(users).search(actor, "  54321  ")
    assert page.items == (target,)
    assert users.telegram_lookups == [54321]
    assert users.search_calls == []


async def test_search_unknown_valid_telegram_id_is_empty_page() -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor, targets={})
    page = await _service(users).search(actor, "99999")
    assert page.items == ()
    assert page.offset == 0
    assert page.limit == 20
    assert users.telegram_lookups == [99999]
    assert users.search_calls == []


async def test_search_numeric_query_offsets_are_paged() -> None:
    actor = _admin()
    target = _user(telegram_user_id=12345)
    users = FakeUsers(fresh=actor, targets={12345: target})
    service = _service(users)
    first = await service.search(actor, "12345", offset=0)
    second = await service.search(actor, "12345", offset=1)
    assert first.items == (target,)
    assert first.offset == 0
    assert second.items == ()
    assert second.offset == 1
    assert users.telegram_lookups == [12345]
    assert users.search_calls == []


@pytest.mark.parametrize("query", ["0", "00", str(2**63), str(2**63 + 7), "9" * 25])
async def test_search_rejects_out_of_range_numeric_query(query: str) -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor, targets={})
    with pytest.raises(ValueError):
        await _service(users).search(actor, query)
    assert users.telegram_lookups == []
    assert users.search_calls == []


@pytest.mark.parametrize("offset", [True, False, 1.5, "0", None])
async def test_search_rejects_boolean_and_non_integer_offsets(offset: object) -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor, targets={12345: _user(telegram_user_id=12345)})
    with pytest.raises(ValueError):
        await _service(users).search(actor, "12345", offset=offset)
    assert users.telegram_lookups == []
    assert users.search_calls == []


async def test_search_caps_offset_to_signed_int64_bounds() -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor)
    await _service(users).search(actor, "ali", offset=2**64)
    assert users.search_calls == [("ali", 2**63 - 1, 20)]


async def test_profile_returns_redacted_wallet_and_server_page() -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    server = _server(owner=target.id)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    wallets = FakeWallets(wallet=Wallet(user_id=target.id, id=uuid4(), balance=123))
    servers = FakeServers(servers=[server], total=7)

    profile = await _service(users, wallets, servers).profile(actor, TARGET_TID)

    assert isinstance(profile, AdminUserProfile)
    assert profile.user is target
    assert profile.wallet is not None
    assert profile.wallet.balance == 123
    assert profile.server_total == 7
    assert profile.server_offset == 0
    assert profile.server_limit == SERVER_PAGE_LIMIT
    assert wallets.get_calls == [target.id]
    assert servers.list_calls == [(target.id, 0, SERVER_PAGE_LIMIT)]

    summary = profile.servers[0]
    assert isinstance(summary, AdminServerSummary)
    assert summary.server_id == server.id
    assert summary.provider_key == server.provider_key
    assert summary.state == server.state.value
    assert summary.created_at == server.created_at
    assert {field.name for field in dataclasses.fields(AdminServerSummary)} == {
        "server_id",
        "provider_key",
        "state",
        "created_at",
    }
    assert not hasattr(summary, "ipv4")
    rendered = repr(summary)
    for secret in (
        _PROVIDER_SERVER_ID,
        str(_PROVIDER_ACCOUNT_ID),
        _CREDENTIAL_ACCOUNT_ID,
        _OFFER_FINGERPRINT_TOKEN,
        _IDEMPOTENCY_KEY,
        _IPV4,
    ):
        assert secret not in rendered


async def test_profile_unknown_target_raises_not_found() -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor, targets={})
    wallets = FakeWallets()
    servers = FakeServers()
    with pytest.raises(UserNotFound):
        await _service(users, wallets, servers).profile(actor, TARGET_TID)
    assert wallets.get_calls == []
    assert servers.list_calls == []


@pytest.mark.parametrize("bad_id", [True, 0, -1, 2**63, 1.5, "224466", None])
async def test_profile_and_set_status_reject_invalid_target_id(bad_id: object) -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    wallets = FakeWallets()
    servers = FakeServers()
    service = _service(users, wallets, servers)

    with pytest.raises(ValueError):
        await service.profile(actor, bad_id)
    with pytest.raises(ValueError):
        await service.set_status(
            actor, bad_id, UserStatus.FROZEN, reason="review", idempotency_key="key"
        )

    assert wallets.get_calls == []
    assert servers.list_calls == []
    assert users.change_status_calls == []


@pytest.mark.parametrize(
    "label",
    [
        "role_user",
        "unconfigured",
        "frozen",
        "banned",
        "forged",
        "fresh_none",
        "fresh_revoked",
        "fresh_identity",
    ],
)
@pytest.mark.parametrize("method", ["search", "profile", "set_status"])
async def test_actor_authorization_rejections(label: str, method: str) -> None:
    actor, users, configured, expect_get = _auth_case(label)
    wallets = FakeWallets()
    servers = FakeServers()
    service = AdminUserService(users, wallets, servers, configured_ids=configured)

    with pytest.raises(PermissionDeniedError):
        await _invoke(service, actor, method)

    assert users.change_status_calls == []
    assert users.create_calls == 0
    assert wallets.get_or_create_calls == 0
    assert wallets.adjust_calls == 0
    assert servers.save_calls == 0
    assert servers.create_calls == 0
    if expect_get:
        assert users.get_calls == [actor.id]
    else:
        assert users.get_calls == []


async def test_set_status_blocks_self_target() -> None:
    actor = _admin()
    users = FakeUsers(fresh=actor, targets={ADMIN_TID: actor})
    with pytest.raises(PermissionDeniedError):
        await _service(users).set_status(
            actor, ADMIN_TID, UserStatus.FROZEN, reason="review", idempotency_key="self"
        )
    assert users.change_status_calls == []


async def test_set_status_blocks_configured_superadmin_target() -> None:
    actor = _admin()
    second = _admin(tid=SECOND_ADMIN_TID)
    users = FakeUsers(fresh=actor, targets={SECOND_ADMIN_TID: second})
    service = AdminUserService(
        users, FakeWallets(), FakeServers(), configured_ids=(ADMIN_TID, SECOND_ADMIN_TID)
    )
    with pytest.raises(PermissionDeniedError):
        await service.set_status(
            actor, SECOND_ADMIN_TID, UserStatus.FROZEN, reason="review", idempotency_key="peer"
        )
    assert users.change_status_calls == []


@pytest.mark.parametrize("reason", ["", "   ", "x" * (MAX_REASON_LENGTH + 1)])
async def test_set_status_requires_bounded_reason(reason: str) -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    with pytest.raises(ValueError):
        await _service(users).set_status(
            actor, TARGET_TID, UserStatus.FROZEN, reason=reason, idempotency_key="key"
        )
    assert users.change_status_calls == []


@pytest.mark.parametrize("key", ["", "  ", "k" * (MAX_IDEMPOTENCY_KEY_LENGTH + 1)])
async def test_set_status_requires_bounded_idempotency_key(key: str) -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    with pytest.raises(ValueError):
        await _service(users).set_status(
            actor, TARGET_TID, UserStatus.FROZEN, reason="review", idempotency_key=key
        )
    assert users.change_status_calls == []


async def test_set_status_accepts_enum_and_string_status() -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    service = _service(users)

    as_enum = await service.set_status(
        actor, TARGET_TID, UserStatus.FROZEN, reason="review", idempotency_key="enum"
    )
    as_string = await service.set_status(
        actor, TARGET_TID, "banned", reason="review", idempotency_key="string"
    )

    assert as_enum is UserStatus.FROZEN
    assert as_string is UserStatus.BANNED


@pytest.mark.parametrize("bad_status", ["nope", 5, True])
async def test_set_status_rejects_unknown_status(bad_status: object) -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    with pytest.raises(ValueError):
        await _service(users).set_status(
            actor, TARGET_TID, bad_status, reason="review", idempotency_key="key"
        )
    assert users.change_status_calls == []


async def test_set_status_delegates_authorized_facts() -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    service = _service(users)

    result = await service.set_status(
        actor,
        TARGET_TID,
        UserStatus.BANNED,
        reason="  abuse  ",
        idempotency_key="  key-1  ",
    )

    assert result is UserStatus.BANNED
    assert len(users.change_status_calls) == 1
    call = users.change_status_calls[0]
    assert call["user_id"] == target.id
    assert call["status"] is UserStatus.BANNED
    assert call["actor_id"] == actor.id
    assert call["idempotency_key"] == "key-1"
    assert call["reason"] == "abuse"
    assert call["admin_configured_ids"] == CONFIGURED
    assert isinstance(call["admin_configured_ids"], tuple)


async def test_double_replay_uses_stable_key_and_never_short_circuits() -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target})
    service = _service(users)

    first = await service.set_status(
        actor, TARGET_TID, UserStatus.FROZEN, reason="review", idempotency_key="stable-key"
    )
    second = await service.set_status(
        actor, TARGET_TID, UserStatus.FROZEN, reason="review", idempotency_key="stable-key"
    )

    assert len(users.change_status_calls) == 2
    keys = [call["idempotency_key"] for call in users.change_status_calls]
    assert keys == ["stable-key", "stable-key"]
    assert users.change_status_calls[0]["status"] is users.change_status_calls[1]["status"]
    assert first is UserStatus.FROZEN
    assert second is UserStatus.FROZEN
    assert len(users.receipts) == 1
    assert target.status is UserStatus.ACTIVE


async def test_no_unexpected_side_effects() -> None:
    actor = _admin()
    target = _user(telegram_user_id=TARGET_TID)
    server = _server(owner=target.id)
    users = FakeUsers(fresh=actor, targets={TARGET_TID: target}, search_result=[target])
    wallets = FakeWallets(wallet=Wallet(user_id=target.id, id=uuid4()))
    servers = FakeServers(servers=[server], total=1)
    service = _service(users, wallets, servers)

    await service.search(actor, "q")
    await service.profile(actor, TARGET_TID)
    await service.set_status(
        actor, TARGET_TID, UserStatus.FROZEN, reason="review", idempotency_key="side"
    )

    assert wallets.get_calls == [target.id]
    assert wallets.get_or_create_calls == 0
    assert wallets.adjust_calls == 0
    assert servers.save_calls == 0
    assert servers.create_calls == 0
    assert users.create_calls == 0


@pytest.mark.parametrize("configured", [(True,), (0,), (-1,)])
def test_construction_rejects_invalid_configured_ids(configured: tuple[int, ...]) -> None:
    with pytest.raises(ValueError):
        AdminUserService(FakeUsers(), FakeWallets(), FakeServers(), configured_ids=configured)
