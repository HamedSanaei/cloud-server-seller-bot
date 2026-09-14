"""Telegram-side transient state for the My Servers flow (PROD-HARDENING §2-§8).

Telegram caps ``callback_data`` at **64 bytes**, while a signed callback already
spends 21 of them on ``v1|`` + ``|`` + the 16-character HMAC. A bare
``servers:detail:<uuid>`` therefore weighs 71 bytes and would be rejected by
Telegram's API. This module removes every long identifier from the wire:

- a **server reference** (8 URL-safe characters) stands in for the local server
  UUID; it is minted per (customer, server) and is the only server identity the
  buttons ever carry;
- a **pending-action nonce** (8 characters) stands in for a whole confirmed
  operation, so the one-time confirmation token — a ~200-character signed blob —
  never travels to Telegram at all;
- a **selection** (image list, snapshot list, ISO list, IP list) is remembered
  per server so a button carries an *index* instead of a provider id. The index
  is resolved against the very list the customer saw, which is what lets the
  confirmation token bind the resolved provider argument rather than the index.

Everything here is presentation state: it holds no provider secret, no
credential and no billing fact, and losing it only means the customer sees the
"that button expired" screen.

Storage. State lives behind the :class:`~cloud_platform.core.session_store.
BotSessionStore` port — Redis in production — so a button still works after a
bot restart and means the same thing on every replica. Each entry is a separate
namespaced, TTL-bounded key:

===========================  =============================================
``ref``                      ``<customer>:<server_id>`` -> opaque reference
``server``                   ``<customer>:<ref>`` -> server id
``selection``                ``<customer>:<ref>:<key>`` -> rendered list
``action``                   ``<customer>:<ref>:<nonce>`` -> pending action
``prompt``                   ``<customer>`` -> outstanding free-text prompt
===========================  =============================================

The reference is only ever resolved back for the **same customer**, so one
customer can never address another customer's server.

Failure policy. When the store is unreachable every method raises
:class:`~cloud_platform.core.session_store.SessionStoreUnavailable`. Callers
must fail **closed**: show the customer a safe "try again shortly" screen and
never execute a mutation. There is deliberately no automatic fall back to
process-local state in production.
"""

from __future__ import annotations

import dataclasses
import secrets
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from cloud_platform.core.session_store import (
    NS_ACTION,
    NS_PROMPT,
    NS_REFERENCE,
    NS_SELECTION,
    NS_SERVER,
    BotSessionStore,
    InMemoryBotSessionStore,
)
from cloud_platform.modules.servers.models import (
    IpAddressView,
    ReinstallImageView,
    ServerOperation,
    ServerSnapshotView,
)

#: Selection items the store may carry. Each is a frozen dataclass of JSON
#: scalars, so a round trip through Redis reconstructs the *typed* model the
#: reader expects (an ``isinstance`` check) instead of a bare string.
_SELECTION_TYPES: dict[str, type[Any]] = {
    cls.__name__: cls for cls in (IpAddressView, ReinstallImageView, ServerSnapshotView)
}

__all__ = [
    "REF_LENGTH",
    "PendingAction",
    "PendingInput",
    "ServerSessions",
]

#: Length of every opaque reference handed to Telegram (URL-safe alphabet, so it
#: always satisfies the callback field pattern ``[A-Za-z0-9._-]+``).
REF_LENGTH = 8

#: How many distinct servers one customer may keep references for.
_MAX_SERVERS_PER_CUSTOMER = 200

#: How many rendered selections a customer/server pair keeps before the oldest
#: are dropped (Redis TTLs do the rest).
_MAX_SELECTIONS = 32


@dataclass(frozen=True, slots=True)
class PendingAction:
    """A confirmed operation waiting for its one ``exec`` callback.

    ``confirmation_token`` is ``None`` for operations the policy does not gate
    (start, reboot, un-null-route, enable monitoring), which still benefit from
    the nonce because a double tap can only reach ``take`` once.
    """

    operation: ServerOperation
    arguments: dict[str, str] = field(default_factory=dict)
    confirmation_token: str | None = None
    #: i18n key of the confirmation body shown on the confirmation screen.
    body_key: str = ""
    #: i18n key of the operation label used in "request sent" wording.
    label_key: str = ""
    #: Customer-facing target line (an IP or a snapshot name — never a secret).
    target: str | None = None

    def to_record(self) -> dict[str, Any]:
        """JSON-safe document stored behind the nonce."""
        return {
            "operation": self.operation.value,
            "arguments": dict(self.arguments),
            "confirmation_token": self.confirmation_token,
            "body_key": self.body_key,
            "label_key": self.label_key,
            "target": self.target,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> PendingAction | None:
        """Rebuild an action, or ``None`` when the record is not usable."""
        try:
            operation = ServerOperation(str(record["operation"]))
        except (KeyError, ValueError, TypeError):
            return None
        raw_arguments = record.get("arguments")
        arguments = (
            {str(key): str(value) for key, value in raw_arguments.items()}
            if isinstance(raw_arguments, dict)
            else {}
        )
        token = record.get("confirmation_token")
        target = record.get("target")
        return cls(
            operation=operation,
            arguments=arguments,
            confirmation_token=None if token is None else str(token),
            body_key=str(record.get("body_key") or ""),
            label_key=str(record.get("label_key") or ""),
            target=None if target is None else str(target),
        )


@dataclass(frozen=True, slots=True)
class PendingInput:
    """A free-text answer the customer still owes the bot."""

    operation: ServerOperation
    server_ref: str
    prompt_key: str
    #: Provider argument name the typed value becomes (``reverse_lookup``...).
    argument: str
    #: Extra provider arguments remembered from the prompting screen.
    extra: dict[str, str] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """JSON-safe document stored behind the prompt key."""
        return {
            "operation": self.operation.value,
            "server_ref": self.server_ref,
            "prompt_key": self.prompt_key,
            "argument": self.argument,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> PendingInput | None:
        """Rebuild a prompt, or ``None`` when the record is not usable."""
        try:
            operation = ServerOperation(str(record["operation"]))
        except (KeyError, ValueError, TypeError):
            return None
        raw_extra = record.get("extra")
        extra = (
            {str(key): str(value) for key, value in raw_extra.items()}
            if isinstance(raw_extra, dict)
            else {}
        )
        return cls(
            operation=operation,
            server_ref=str(record.get("server_ref") or ""),
            prompt_key=str(record.get("prompt_key") or ""),
            argument=str(record.get("argument") or ""),
            extra=extra,
        )


class ServerSessions:
    """Shared, TTL-bounded presentation state for the My Servers flow.

    Every method is a coroutine because the backend is a network store. The
    class is deliberately free of module-level mutable state, so two bot
    replicas pointing at the same store behave identically.
    """

    def __init__(
        self,
        store: BotSessionStore | None = None,
        *,
        reference_ttl_seconds: int = 1800,
        prompt_ttl_seconds: int = 900,
    ) -> None:
        if reference_ttl_seconds < 30:
            raise ValueError("reference_ttl_seconds must be >= 30")
        if prompt_ttl_seconds < 30:
            raise ValueError("prompt_ttl_seconds must be >= 30")
        self._store: BotSessionStore = store or InMemoryBotSessionStore()
        self._reference_ttl = reference_ttl_seconds
        self._prompt_ttl = prompt_ttl_seconds

    # -- server references -------------------------------------------------

    async def ref_for(self, customer_id: UUID, server_id: UUID) -> str:
        """The stable reference this customer's server is addressed by."""
        forward = f"{customer_id}:{server_id}"
        existing = await self._store.get(NS_REFERENCE, forward)
        if existing is not None and existing.get("ref"):
            ref = str(existing["ref"])
            # Refresh the window so a customer navigating through screens does
            # not lose the buttons they are holding.
            await self._store.put(
                NS_REFERENCE, forward, {"ref": ref}, ttl_seconds=self._reference_ttl
            )
            await self._store.put(
                NS_SERVER,
                f"{customer_id}:{ref}",
                {"server_id": str(server_id)},
                ttl_seconds=self._reference_ttl,
            )
            return ref
        ref = await self._mint_unique_ref(customer_id)
        await self._store.put(NS_REFERENCE, forward, {"ref": ref}, ttl_seconds=self._reference_ttl)
        await self._store.put(
            NS_SERVER,
            f"{customer_id}:{ref}",
            {"server_id": str(server_id)},
            ttl_seconds=self._reference_ttl,
        )
        return ref

    async def _mint_unique_ref(self, customer_id: UUID) -> str:
        """Mint a reference no other server of this customer currently holds."""
        for _ in range(6):
            ref = _mint_ref()
            if not await self._store.exists(NS_SERVER, f"{customer_id}:{ref}"):
                return ref
        # Astronomically unlikely; widen the token rather than reuse a ref.
        return secrets.token_urlsafe(12)[:24]

    async def server_id(self, customer_id: UUID, ref: str) -> UUID | None:
        """The server a reference resolves to, for THIS customer only.

        The key embeds the customer id and the reference, so a reference minted
        for another customer is simply absent here — never a cross-customer hit.
        """
        if not ref or ":" in ref:
            return None
        record = await self._store.get(NS_SERVER, f"{customer_id}:{ref}")
        return _server_id_from_record(record)

    # -- selections --------------------------------------------------------

    async def remember(self, customer_id: UUID, ref: str, key: str, items: tuple[Any, ...]) -> None:
        """Remember the list rendered under ``key`` for one server.

        Only JSON scalars are kept (index -> id/label pairs), so a remembered
        list can never smuggle a secret into Redis.
        """
        record = {"items": [_encode_item(item) for item in list(items)[-_MAX_SELECTIONS:]]}
        await self._store.put(
            NS_SELECTION,
            f"{customer_id}:{ref}:{key}",
            record,
            ttl_seconds=self._reference_ttl,
        )

    async def selection(self, customer_id: UUID, ref: str, key: str) -> tuple[Any, ...]:
        """The remembered list, or an empty tuple when there is none."""
        record = await self._store.get(NS_SELECTION, f"{customer_id}:{ref}:{key}")
        if record is None:
            return ()
        items = record.get("items")
        if not isinstance(items, list):
            return ()
        return tuple(_decode_item(item) for item in items)

    # -- pending confirmations ---------------------------------------------

    async def stash(self, customer_id: UUID, ref: str, nonce: str, action: PendingAction) -> bool:
        """Store ``action`` under ``nonce``.

        The caller derives ``nonce`` deterministically from the operation and
        its arguments, so re-confirming the SAME operation overwrites one slot
        instead of creating a second live path to the provider: two taps of
        "confirm", or two taps of "execute", can only ever consume one action.
        """
        await self._store.put(
            NS_ACTION,
            f"{customer_id}:{ref}:{nonce}",
            action.to_record(),
            ttl_seconds=self._reference_ttl,
        )
        return True

    async def take(self, customer_id: UUID, ref: str, nonce: str) -> PendingAction | None:
        """Consume a pending action exactly once (None on replay/expiry).

        ``store.take`` is a single atomic read-and-delete on the backend, so of
        two concurrent ``servers:exec`` deliveries exactly one observes the
        action — and therefore exactly one provider mutation is issued.
        """
        record = await self._store.take(NS_ACTION, f"{customer_id}:{ref}:{nonce}")
        return None if record is None else PendingAction.from_record(record)

    # -- free-text prompts -------------------------------------------------

    async def await_input(self, customer_id: UUID, ref: str, pending: PendingInput) -> bool:
        """Remember that the next message from this customer answers a prompt."""
        await self._store.put(
            NS_PROMPT, str(customer_id), pending.to_record(), ttl_seconds=self._prompt_ttl
        )
        return True

    async def take_input(self, customer_id: UUID) -> PendingInput | None:
        """Consume the customer's outstanding prompt (single use)."""
        record = await self._store.take(NS_PROMPT, str(customer_id))
        return None if record is None else PendingInput.from_record(record)

    # -- resilience --------------------------------------------------------

    async def drop_customer(self, customer_id: UUID) -> None:
        """Forget the outstanding prompt for a customer (fail-closed helper)."""
        await self._store.delete(NS_PROMPT, str(customer_id))


_TAG = "__t__"
_VALUE = "v"


def _encode_item(item: Any) -> Any:
    """Serialize a remembered selection item, tagging anything non-scalar.

    Tagging is what makes the round trip lossless: expiry or corruption then
    degrades to ``None`` (and the UI's "this button expired" screen) instead of
    silently substituting a string for a typed model.
    """
    if item is None or isinstance(item, str | int | float | bool):
        return item
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        return {
            _TAG: type(item).__name__,
            _VALUE: {
                info.name: _encode_item(getattr(item, info.name))
                for info in dataclasses.fields(item)
            },
        }
    if isinstance(item, tuple | list):
        return {_TAG: "list", _VALUE: [_encode_item(value) for value in item]}
    if isinstance(item, dict):
        return {
            _TAG: "dict",
            _VALUE: {str(key): _encode_item(value) for key, value in item.items()},
        }
    # Unknown object: never persist its repr (it could contain anything).
    return {_TAG: "list", _VALUE: []}


def _decode_item(payload: Any) -> Any:
    """Reconstruct a remembered item; ``None`` when it cannot be trusted."""
    if isinstance(payload, list):
        return [_decode_item(value) for value in payload]
    if not isinstance(payload, dict):
        return payload
    tag = payload.get(_TAG)
    value = payload.get(_VALUE)
    if tag == "list":
        return [_decode_item(item) for item in value] if isinstance(value, list) else None
    if tag == "dict":
        if not isinstance(value, dict):
            return None
        return {str(key): _decode_item(item) for key, item in value.items()}
    model = _SELECTION_TYPES.get(str(tag))
    if model is None or not isinstance(value, dict):
        return None
    try:
        return model(**{str(key): _decode_item(item) for key, item in value.items()})
    except TypeError:  # pragma: no cover - shape from an older schema
        return None


def _server_id_from_record(record: dict[str, Any] | None) -> UUID | None:
    """Extract the server id from a reference record, tolerating old shapes."""
    if record is None:
        return None
    raw = record.get("server_id")
    if raw is None:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _mint_ref() -> str:
    """A fresh URL-safe reference (collisions are handled by the key layout)."""
    return secrets.token_urlsafe(6)[:REF_LENGTH]
