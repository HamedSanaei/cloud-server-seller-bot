"""Redis-backed transient state (PROD-HARDENING §2-§11, §43-§44).

Nothing here touches a real Redis: a small fake implements exactly the command
surface :class:`RedisBotSessionStore` uses, including the two server-side
scripts, so the *atomicity* semantics are what the tests actually exercise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from cloud_platform.bot.sessions import (
    PendingAction,
    PendingInput,
    ServerSessions,
)
from cloud_platform.core.session_store import (
    NS_ACTION,
    NS_CONFIRMATION,
    NS_PROMPT,
    NS_REFERENCE,
    NS_SELECTION,
    NS_SERVER,
    BotSessionStore,
    InMemoryBotSessionStore,
    RedisBotSessionStore,
    SessionStoreUnavailable,
    build_session_store,
    decode_session_value,
    encode_session_value,
    session_key,
)
from cloud_platform.modules.servers.confirmations import (
    ConfirmationBinding,
    ConfirmationStatus,
    ConfirmationVerifier,
    InMemoryConfirmationStore,
    SharedConfirmationStore,
)
from cloud_platform.modules.servers.models import (
    IpAddressView,
    ServerOperation,
    ServerSnapshotView,
)

#: Fixed ids keep failures readable and the assertions deterministic.
CUSTOMER = UUID("11111111-1111-1111-1111-111111111111")
OTHER_CUSTOMER = UUID("22222222-2222-2222-2222-222222222222")
SERVER_ID = UUID("33333333-3333-3333-3333-333333333333")
OTHER_SERVER_ID = UUID("44444444-4444-4444-4444-444444444444")
SIGNING_KEY = "unit-test-signing-key"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRedis:
    """The narrow Redis surface the store uses, with real script semantics.

    ``eval`` recognises the two scripts by shape, so a test that claims a key
    twice sees the same single-winner behaviour a real Redis gives.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, name: str) -> Any:
        return self.values.get(name)

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> Any:
        if nx and name in self.values:
            return None
        self.values[name] = value
        if ex is not None:
            self.expiries[name] = int(ex)
        return True

    async def delete(self, *names: str) -> Any:
        removed = 0
        for name in names:
            if self.values.pop(name, None) is not None:
                removed += 1
            self.expiries.pop(name, None)
        return removed

    async def exists(self, *names: str) -> Any:
        return sum(1 for name in names if name in self.values)

    async def expire(self, name: str, time: int) -> Any:
        if name in self.values:
            self.expiries[name] = int(time)
            return True
        return False

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        assert numkeys == 1
        key = str(keys_and_args[0])
        if "EXISTS" in script:  # the atomic "set only if absent" claim
            value, ttl = str(keys_and_args[1]), str(keys_and_args[2])
            if key in self.values:
                return 0
            self.values[key] = value
            self.expiries[key] = int(ttl)
            return 1
        # the atomic read-and-delete
        return self.values.pop(key, None)

    async def ping(self) -> Any:
        return True

    async def aclose(self) -> Any:
        return None


class UnreachableRedis(FakeRedis):
    """A Redis that refuses every command (outage / network partition)."""

    def _boom(self) -> None:
        raise ConnectionError("connection refused")

    async def get(self, name: str) -> Any:
        self._boom()

    async def set(self, name: str, value: str, *, ex: int | None = None, nx: bool = False) -> Any:
        self._boom()

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        self._boom()

    async def ping(self) -> Any:
        self._boom()


def make_redis_store(client: Any | None = None) -> RedisBotSessionStore:
    return RedisBotSessionStore(client or FakeRedis(), prefix="cloud-platform:bot")


# ---------------------------------------------------------------------------
# Key layout + TTL
# ---------------------------------------------------------------------------


class TestKeyLayout:
    def test_keys_are_namespaced_and_versioned(self) -> None:
        key = session_key("cloud-platform:bot", NS_REFERENCE, "customer", "server")
        assert key == "cloud-platform:bot:ref:v1:customer:server"

    async def test_writes_carry_a_ttl(self) -> None:
        client = FakeRedis()
        store = RedisBotSessionStore(client, prefix="cloud-platform:bot")
        await store.put(NS_REFERENCE, "abc", {"ref": "xyz"}, ttl_seconds=1800)

        stored_key = "cloud-platform:bot:ref:v1:abc"
        assert client.values[stored_key] == encode_session_value({"ref": "xyz"})
        assert client.expiries[stored_key] == 1800

    async def test_non_positive_ttl_is_refused(self) -> None:
        store = make_redis_store()
        with pytest.raises(ValueError):
            await store.put(NS_REFERENCE, "abc", {"ref": "x"}, ttl_seconds=0)

    async def test_a_secret_is_never_part_of_a_key(self) -> None:
        """Keys embed only identifiers, never a credential or a console URL."""
        key = session_key("cloud-platform:bot", NS_PROMPT, str(CUSTOMER))
        assert "http" not in key
        assert "password" not in key
        # prefix(2) + namespace + version + identifier
        assert key.count(":") == 4


class TestPayloadSafety:
    def test_malformed_json_is_rejected(self) -> None:
        assert decode_session_value("{not json") is None

    def test_unknown_schema_version_is_rejected(self) -> None:
        payload = json.dumps({"schema": "v99", "value": {"ref": "x"}})
        assert decode_session_value(payload) is None

    def test_payload_without_envelope_is_rejected(self) -> None:
        assert decode_session_value(json.dumps({"ref": "x"})) is None

    async def test_a_corrupt_record_reads_as_absent(self) -> None:
        client = FakeRedis()
        store = RedisBotSessionStore(client, prefix="cloud-platform:bot")
        client.values["cloud-platform:bot:ref:v1:abc"] = "garbage"
        assert await store.get(NS_REFERENCE, "abc") is None


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


class TestAtomicPrimitives:
    async def test_claim_only_ever_succeeds_once(self) -> None:
        store = make_redis_store()
        assert await store.claim(NS_CONFIRMATION, "n1", {"value": "1"}, ttl_seconds=60)
        assert not await store.claim(NS_CONFIRMATION, "n1", {"value": "1"}, ttl_seconds=60)
        assert await store.exists(NS_CONFIRMATION, "n1")

    async def test_claim_is_not_disturbed_by_a_different_key(self) -> None:
        store = make_redis_store()
        assert await store.claim(NS_CONFIRMATION, "n1", {"value": "1"}, ttl_seconds=60)
        assert await store.claim(NS_CONFIRMATION, "n2", {"value": "1"}, ttl_seconds=60)

    async def test_take_is_a_single_read_and_delete(self) -> None:
        store = make_redis_store()
        await store.put(NS_ACTION, "k", {"operation": "reboot"}, ttl_seconds=60)
        assert await store.take(NS_ACTION, "k") == {"operation": "reboot"}
        assert await store.take(NS_ACTION, "k") is None


# ---------------------------------------------------------------------------
# Failure policy (§9)
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_unreachable_store_raises_a_typed_error(self) -> None:
        store = RedisBotSessionStore(UnreachableRedis(), prefix="cloud-platform:bot")
        with pytest.raises(SessionStoreUnavailable):
            await store.get(NS_REFERENCE, "abc")

    async def test_error_message_carries_no_payload_or_connection_string(self) -> None:
        store = RedisBotSessionStore(UnreachableRedis(), prefix="cloud-platform:bot")
        with pytest.raises(SessionStoreUnavailable) as caught:
            await store.put(NS_REFERENCE, "abc", {"ref": "secret-ref"}, ttl_seconds=60)
        assert "secret-ref" not in str(caught.value)
        assert "connection refused" not in str(caught.value)

    async def test_redis_url_is_not_echoed_on_failure(self) -> None:
        store = RedisBotSessionStore(UnreachableRedis(), prefix="cloud-platform:bot")
        with pytest.raises(SessionStoreUnavailable) as caught:
            await store.ping()
        assert "redis://" not in str(caught.value)

    async def test_unreachable_store_refuses_a_confirmation(self) -> None:
        """The whole point: no proving a token is unused ⇒ no execution."""
        verifier = ConfirmationVerifier(
            SIGNING_KEY,
            store=SharedConfirmationStore(RedisBotSessionStore(UnreachableRedis())),
        )
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.REINSTALL)
        token = verifier.issue(binding)
        result = await verifier.consume(token.token, binding)
        assert result.status is ConfirmationStatus.UNAVAILABLE
        assert not result.ok

    async def test_unreachable_store_refuses_a_session_lookup(self) -> None:
        sessions = ServerSessions(RedisBotSessionStore(UnreachableRedis()))
        with pytest.raises(SessionStoreUnavailable):
            await sessions.server_id(CUSTOMER, "abcdefgh")


class TestBackendSelection:
    def test_memory_backend_is_refused_outside_development(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A production deployment must never silently run on process-local state."""
        from cloud_platform.core.config import get_settings, reset_settings_cache

        monkeypatch.setenv("APP_ENV", "production")
        reset_settings_cache()
        try:
            with pytest.raises(ValueError, match="in-memory"):
                build_session_store(
                    backend="memory", prefix="cloud-platform:bot", redis_url="redis://x/0"
                )
        finally:
            monkeypatch.delenv("APP_ENV", raising=False)
            reset_settings_cache()
        assert get_settings() is not None

    def test_unknown_backend_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown"):
            build_session_store(backend="sqlite", prefix="p", redis_url="redis://x/0")

    def test_redis_backend_builds_a_redis_store(self) -> None:
        store = build_session_store(
            backend="redis", prefix="cloud-platform:bot", redis_url="redis://localhost:6379/0"
        )
        assert isinstance(store, RedisBotSessionStore)
        assert store.client is not None


# ---------------------------------------------------------------------------
# ServerSessions over the shared store
# ---------------------------------------------------------------------------


class TestServerSessionsOnSharedStore:
    async def test_reference_is_stable_and_per_customer(self) -> None:
        store = make_redis_store()
        sessions = ServerSessions(store)
        ref = await sessions.ref_for(CUSTOMER, SERVER_ID)
        assert await sessions.ref_for(CUSTOMER, SERVER_ID) == ref
        assert await sessions.server_id(CUSTOMER, ref) == SERVER_ID
        # A reference minted for another customer never resolves here.
        assert await sessions.server_id(OTHER_CUSTOMER, ref) is None

    async def test_reference_keys_are_namespaced(self) -> None:
        client = FakeRedis()
        sessions = ServerSessions(RedisBotSessionStore(client, prefix="cloud-platform:bot"))
        await sessions.ref_for(CUSTOMER, SERVER_ID)
        keys = set(client.values)
        assert f"cloud-platform:bot:ref:v1:{CUSTOMER}:{SERVER_ID}" in keys
        assert any(key.startswith(f"cloud-platform:bot:server:v1:{CUSTOMER}:") for key in keys)

    async def test_typeless_remembered_items_round_trip_as_models(self) -> None:
        sessions = ServerSessions(make_redis_store())
        ref = await sessions.ref_for(CUSTOMER, SERVER_ID)
        snapshot = ServerSnapshotView(ref="snap-1", name="before-upgrade", state="available")
        await sessions.remember(CUSTOMER, ref, "snapshots", (snapshot,))
        items = await sessions.selection(CUSTOMER, ref, "snapshots")
        assert items == (snapshot,)

    async def test_ip_items_keep_their_type_across_the_store(self) -> None:
        sessions = ServerSessions(make_redis_store())
        ref = await sessions.ref_for(CUSTOMER, SERVER_ID)
        address = IpAddressView(ip="88.1.2.3", version=4, main_ip=True, null_routed=False)
        await sessions.remember(CUSTOMER, ref, "ips", (address,))
        items = await sessions.selection(CUSTOMER, ref, "ips")
        assert isinstance(items[0], IpAddressView)

    async def test_tuple_selections_survive_the_round_trip(self) -> None:
        sessions = ServerSessions(make_redis_store())
        ref = await sessions.ref_for(CUSTOMER, SERVER_ID)
        await sessions.remember(CUSTOMER, ref, "isos", (("iso-1", "Debian 13"),))
        iso_ref, name = (await sessions.selection(CUSTOMER, ref, "isos"))[0]
        assert (iso_ref, name) == ("iso-1", "Debian 13")

    async def test_pending_actions_are_single_use_across_instances(self) -> None:
        client = FakeRedis()
        first = ServerSessions(make_redis_store(client))
        second = ServerSessions(make_redis_store(client))
        ref = await first.ref_for(CUSTOMER, SERVER_ID)
        action = PendingAction(operation=ServerOperation.REBOOT, arguments={"why": "test"})
        await first.stash(CUSTOMER, ref, "nonce1234", action)

        taken = await second.take(CUSTOMER, ref, "nonce1234")
        assert taken is not None and taken.operation is ServerOperation.REBOOT
        assert taken.arguments == {"why": "test"}
        # The first replica can no longer reach it: exactly one executor.
        assert await first.take(CUSTOMER, ref, "nonce1234") is None

    async def test_prompt_is_single_use_across_instances(self) -> None:
        client = FakeRedis()
        first = ServerSessions(make_redis_store(client))
        second = ServerSessions(make_redis_store(client))
        ref = await first.ref_for(CUSTOMER, SERVER_ID)
        pending = PendingInput(
            operation=ServerOperation.RENAME,
            server_ref=ref,
            prompt_key="servers.rename_prompt",
            argument="display_name",
        )
        await first.await_input(CUSTOMER, ref, pending)

        taken = await second.take_input(CUSTOMER)
        assert taken is not None and taken.operation is ServerOperation.RENAME
        assert await first.take_input(CUSTOMER) is None


# ---------------------------------------------------------------------------
# Confirmations: restart + multi-instance (§6-§8)
# ---------------------------------------------------------------------------


class TestConfirmationDurability:
    def _verifier(self, store: Any, *, ttl: int = 300) -> ConfirmationVerifier:
        return ConfirmationVerifier(
            SIGNING_KEY, store=SharedConfirmationStore(store), ttl_seconds=ttl
        )

    async def test_confirmation_survives_a_restart(self) -> None:
        client = FakeRedis()
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.SNAPSHOT_DELETE)

        # Bot instance A issues the confirmation the customer sees...
        issued = self._verifier(make_redis_store(client)).issue(binding)
        # ...then the container is replaced before the customer taps confirm.
        restarted = self._verifier(make_redis_store(client))
        result = await restarted.consume(issued.token, binding)
        assert result.status is ConfirmationStatus.OK

    async def test_another_replica_can_consume_the_token(self) -> None:
        client = FakeRedis()
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.REINSTALL)
        issued = self._verifier(make_redis_store(client)).issue(binding)
        other_replica = self._verifier(make_redis_store(client))
        assert (await other_replica.consume(issued.token, binding)).status is ConfirmationStatus.OK

    async def test_replay_from_the_issuing_replica_is_refused(self) -> None:
        client = FakeRedis()
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.PASSWORD_RESET)
        issuer = self._verifier(make_redis_store(client))
        issued = issuer.issue(binding)

        assert (await issuer.consume(issued.token, binding)).status is ConfirmationStatus.OK
        replay = await issuer.consume(issued.token, binding)
        assert replay.status is ConfirmationStatus.REPLAYED
        assert not replay.ok

    async def test_double_click_across_two_replicas_executes_once(self) -> None:
        """Two simultaneous deliveries must yield exactly one OK."""
        client = FakeRedis()
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.SNAPSHOT_RESTORE)
        issued = self._verifier(make_redis_store(client)).issue(binding)
        replica_a = self._verifier(make_redis_store(client))
        replica_b = self._verifier(make_redis_store(client))

        first = await replica_a.consume(issued.token, binding)
        second = await replica_b.consume(issued.token, binding)
        statuses = {first.status, second.status}
        assert statuses == {ConfirmationStatus.OK, ConfirmationStatus.REPLAYED}

    async def test_binding_mismatch_is_refused_without_consuming(self) -> None:
        client = FakeRedis()
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.SNAPSHOT_DELETE)
        verifier = self._verifier(make_redis_store(client))
        issued = verifier.issue(binding)

        attacker = ConfirmationBinding(
            CUSTOMER, SERVER_ID, ServerOperation.SNAPSHOT_DELETE, {"snapshot": "other"}
        )
        assert (await verifier.consume(issued.token, attacker)).status is (
            ConfirmationStatus.MISMATCH
        )
        # A rejected attempt must not burn the legitimate token.
        assert (await verifier.consume(issued.token, binding)).status is ConfirmationStatus.OK

    async def test_wrong_customer_and_server_are_refused(self) -> None:
        client = FakeRedis()
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.REINSTALL)
        verifier = self._verifier(make_redis_store(client))
        issued = verifier.issue(binding)

        for wrong in (
            ConfirmationBinding(OTHER_CUSTOMER, SERVER_ID, ServerOperation.REINSTALL),
            ConfirmationBinding(CUSTOMER, OTHER_SERVER_ID, ServerOperation.REINSTALL),
            ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.SNAPSHOT_DELETE),
        ):
            assert (await verifier.consume(issued.token, wrong)).status is (
                ConfirmationStatus.MISMATCH
            )

    async def test_expiry_is_reported_before_consumption(self) -> None:
        client = FakeRedis()
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.REINSTALL)
        verifier = self._verifier(make_redis_store(client), ttl=300)
        issued = verifier.issue(binding)
        later = datetime.now(UTC) + timedelta(seconds=301)

        assert (await verifier.consume(issued.token, binding, now=later)).status is (
            ConfirmationStatus.EXPIRED
        )
        assert not await SharedConfirmationStore(make_redis_store(client)).is_consumed(issued.nonce)

    async def test_tampered_token_is_invalid(self) -> None:
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.REINSTALL)
        verifier = self._verifier(make_redis_store())
        issued = verifier.issue(binding)
        body = issued.token.split(".")
        body[1] = body[1][:-1] + ("A" if body[1][-1] != "A" else "B")
        assert (
            await verifier.consume(".".join(body), binding)
        ).status is ConfirmationStatus.INVALID

    async def test_the_replay_marker_outlives_the_token(self) -> None:
        """A consumed token stays distinguishable from a never-existing one."""
        client = FakeRedis()
        store = make_redis_store(client)
        verifier = self._verifier(store, ttl=60)
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.REINSTALL)
        issued = verifier.issue(binding)
        await verifier.consume(issued.token, binding)

        marker = f"cloud-platform:bot:confirm:v1:{issued.nonce}"
        assert marker in client.values
        # TTL = max(remaining, replay window), so it never expires early.
        assert client.expiries[marker] >= 1800

    async def test_in_memory_store_still_works_for_single_process(self) -> None:
        verifier = ConfirmationVerifier(
            SIGNING_KEY, store=InMemoryConfirmationStore(), ttl_seconds=120
        )
        binding = ConfirmationBinding(CUSTOMER, SERVER_ID, ServerOperation.REBOOT)
        issued = verifier.issue(binding)
        assert (await verifier.consume(issued.token, binding)).status is ConfirmationStatus.OK
        assert (await verifier.consume(issued.token, binding)).status is ConfirmationStatus.REPLAYED


class TestSessionStoreContract:
    """Both adapters must satisfy the same port (protocol conformance)."""

    @pytest.mark.parametrize(
        "store",
        [InMemoryBotSessionStore(), make_redis_store()],
        ids=["memory", "redis"],
    )
    async def test_both_adapters_implement_the_port(self, store: BotSessionStore) -> None:
        await store.put(NS_SELECTION, "k", {"items": []}, ttl_seconds=60)
        assert await store.get(NS_SELECTION, "k") == {"items": []}
        assert await store.exists(NS_SELECTION, "k")
        assert await store.claim(NS_CONFIRMATION, "n", {"value": "1"}, ttl_seconds=60)
        assert not await store.claim(NS_CONFIRMATION, "n", {"value": "1"}, ttl_seconds=60)
        await store.delete(NS_SELECTION, "k")
        assert not await store.exists(NS_SELECTION, "k")
        await store.ping()

    async def test_memory_store_loses_state_on_clear(self) -> None:
        store = InMemoryBotSessionStore()
        await store.put(NS_REFERENCE, "abc", {"ref": "x"}, ttl_seconds=60)
        store.clear()
        assert await store.get(NS_REFERENCE, "abc") is None

    async def test_memory_store_ttl_refuses_non_positive(self) -> None:
        store = InMemoryBotSessionStore()
        with pytest.raises(ValueError):
            await store.put(NS_REFERENCE, "abc", {"ref": "x"}, ttl_seconds=0)

    async def test_key_isolation_by_namespace(self) -> None:
        store = InMemoryBotSessionStore()
        await store.put(NS_REFERENCE, "abc", {"ref": "x"}, ttl_seconds=60)
        assert await store.get(NS_SERVER, "abc") is None
        await store.put(NS_SERVER, "abc", {"server_id": str(uuid4())}, ttl_seconds=60)
        assert await store.get(NS_REFERENCE, "abc") == {"ref": "x"}


class TestKeyCollisionSafety:
    async def test_references_do_not_collide_between_customers(self) -> None:
        client = FakeRedis()
        sessions = ServerSessions(make_redis_store(client))
        mine = await sessions.ref_for(CUSTOMER, SERVER_ID)
        theirs = await sessions.ref_for(OTHER_CUSTOMER, OTHER_SERVER_ID)
        assert await sessions.server_id(CUSTOMER, mine) == SERVER_ID
        assert await sessions.server_id(OTHER_CUSTOMER, theirs) == OTHER_SERVER_ID
        # Even a shared literal reference cannot cross a customer boundary.
        assert await sessions.server_id(OTHER_CUSTOMER, mine) is None

    async def test_a_guessed_reference_does_not_resolve(self) -> None:
        sessions = ServerSessions(make_redis_store())
        await sessions.ref_for(CUSTOMER, SERVER_ID)
        assert await sessions.server_id(CUSTOMER, "AAAAAAAA") is None

    async def test_reference_containing_a_separator_is_refused(self) -> None:
        sessions = ServerSessions(make_redis_store())
        assert await sessions.server_id(CUSTOMER, "a:b") is None

    async def test_minted_references_stay_within_the_callback_budget(self) -> None:
        sessions = ServerSessions(make_redis_store())
        ref = await sessions.ref_for(CUSTOMER, SERVER_ID)
        assert len(ref) == 8
        assert set(ref) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
