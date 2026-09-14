# Telegram session storage

Where the bot keeps the **transient state** behind its buttons and prompts, why
it lives in Redis in production, what happens when Redis is unreachable, and the
one deployment rule Telegram itself imposes on us.

Read this together with [`SERVER_MANAGEMENT.md`](SERVER_MANAGEMENT.md) (the
customer flow) and [`../commerce/RENEWAL_LIFECYCLE.md`](../commerce/RENEWAL_LIFECYCLE.md)
(the commercial state that some of those screens display).

---

## 1. Why not process memory

Telegram gives a callback button 64 bytes of `callback_data`. A local server id
is a UUID (36 characters) and a signed callback already spends 21 of them, so
the bot never puts an identifier on the wire. It puts an **opaque short
reference** and resolves it server-side.

That resolution state — plus remembered selections, pending confirmations,
pending text prompts and consumed-confirmation markers — used to live in a
per-process `dict`. That is unsafe for every real deployment:

| Event | Process-local state | Shared state (Redis) |
| --- | --- | --- |
| container restart / redeploy | every open button dies, confirmations vanish | references and confirmations survive |
| rolling deploy (two processes for a moment) | the two disagree about what a reference means | both read the same value |
| second replica | cannot resolve another replica's buttons | resolves them |
| crash between confirmation and execution | the token may be replayable | the claim is already recorded |

So the production backend is Redis, and the bot is written against a **port**
rather than against Redis commands.

---

## 2. Architecture

```text
Telegram UI (bot/servers_ui.py, bot/monthly_ui.py, bot/sessions.py)
        │  put / get / claim / take / delete / exists
        ▼
BotSessionStore                       core/session_store.py   (the port)
        ├── RedisBotSessionStore      ← production
        └── InMemoryBotSessionStore   ← tests, and `memory` outside production
        ▼
create_redis_client()                 core/redis.py           (ONE client factory)
```

* `core/redis.py` is the **only** place a Redis client is constructed
  (`decode_responses=True`), and the only place it is closed. Readiness probes
  (`_check_redis`) and the doctor command use it too.
* `core/session_store.py` defines the port, the key layout, the payload
  envelope, the atomic primitives and the failure type.
* `bot/sessions.py` (`ServerSessions`) is the typed façade the UI uses: server
  references, remembered selections, single-use pending actions and text
  prompts.
* `modules/servers/confirmations.py` uses the same store for
  `SharedConfirmationStore` — the single-use confirmation marker.

Nothing in the UI imports Redis. Nothing in the UI builds a Redis key.

---

## 3. Key namespaces and TTLs

Every key is built by `session_key()`:

```text
<namespace>:<family>:v1[:<part>…]
```

e.g. `cloud-platform:bot:ref:v1:<telegram_user_id>:<reference>`. The literal
`v1` is the **schema version** and appears in the key *and* in the payload, so a
future incompatible change can roll out without misreading old values: old keys
simply expire.

| Family (constant) | Stores | TTL | Setting |
| --- | --- | --- | --- |
| `ref` | forward lookup `(customer, server) → reference` | 1800 s | `[telegram.sessions] reference_ttl_seconds` |
| `server` | reverse lookup `(customer, reference) → server`, the only way a button resolves | 1800 s | same |
| `selection` | a remembered list (images, snapshots, ISOs, IPs) so a button can carry an index | 1800 s | same |
| `action` | a pending, **already confirmed** action (operation + arguments + token) | 1800 s | same |
| `prompt` | a free-text prompt waiting for the customer's next message (rename, rDNS) | 900 s | `prompt_ttl_seconds` |
| `confirm` | the single-use marker for a consumed confirmation nonce | `max(remaining token lifetime, replay window)` | `replay_ttl_seconds` |
| `replay` | reserved for explicit replay markers | 1800 s | `replay_ttl_seconds` |

The confirmation token's own lifetime comes from the operator policy
(`[features.server_management] confirmation_ttl_seconds`, default 900 s), not
from `[telegram.sessions]`. Only the *replay window* is a session setting — see
the container wiring in `core/container.py`, where that split is deliberate so
an unrelated Telegram tuning value can never shorten a confirmation.

**Key hygiene.** A key part is always a UUID, an 8-character minted reference, a
nonce or a small integer. Provider ids, IPs, free text, console URLs,
credentials and API keys are never interpolated into a key name, and a
reference containing a separator is rejected rather than silently mangled
(`tests/unit/test_session_store.py::TestKeyCollisionSafety`).

**Payload hygiene.** Values are JSON we wrote ourselves:

```json
{"schema": "v1", "value": {"...": "..."}}
```

`decode_session_value()` rejects malformed JSON, a missing envelope, a
non-object payload and an unknown schema version. A rejected payload reads as
*absent* (the customer sees "this button expired") and is logged by kind only —
never dumped.

---

## 4. Atomic consumption

Two concurrent taps must not both win. The store exposes two server-side
primitives so the race is decided **inside Redis**, not between two round trips:

* `claim(family, key, value, ttl_seconds)` — `SET … NX EX …` in a Lua script;
  returns `1` only for the caller that won.
* `take(family, key)` — `GET` + `DEL` in a single script (GETDEL semantics,
  independent of the Redis server version).

The confirmation path uses `claim`. `SharedConfirmationStore.consume()` writes
one marker per nonce with a TTL covering the token's remaining life **and** the
replay window, so:

* the first consumption wins in Redis;
* a restart cannot resurrect a consumed token;
* every later attempt answers `REPLAYED`.

There is deliberately **no** `get` followed by a separate `delete` anywhere on a
one-time path — that sequence is a race and is not used.

`bot/sessions.py` uses `take` for single-use pending actions and prompts: the
action that a confirmation screen produced is destroyed as it is read, so a
redelivered callback finds nothing and answers "already in progress" instead of
mutating twice.

---

## 5. Outcomes and what the customer sees

`ConfirmationStatus` distinguishes the failure modes so the wording is honest:

| Status | Meaning | Customer sees |
| --- | --- | --- |
| `OK` | first and only consumption | the operation runs |
| `EXPIRED` | past its expiry, never consumed | "the confirmation expired, please start again" |
| `REPLAYED` | already consumed | "this request was already processed" |
| `MISMATCH` | token does not belong to this customer/server/operation/arguments | "not allowed" |
| `INVALID` | malformed or tampered | "not allowed" |
| `UNAVAILABLE` | the shared store could not be reached | "temporarily unavailable, try again shortly" — **and nothing runs** |

A consumed token is therefore *not* indistinguishable from a never-existing one,
which is what makes the double-click UX sensible rather than confusing.

---

## 6. Multi-instance and restart guarantees

`tests/unit/test_session_store.py` builds two independent
`RedisBotSessionStore` instances over one shared fake Redis and proves:

* a confirmation created by instance A is consumed by instance B;
* the replay is refused by A after B consumed it;
* a double-click delivered to two replicas executes the provider mutation
  **once**;
* a session lookup and a confirmation created on one replica resolve on the
  other.

Because the store is the only shared state on these paths, those guarantees hold
for a restart, a container replacement and a rolling deploy. Nothing in the flow
relies on a module-level global for correctness.

---

## 7. Redis outage policy (fail closed)

When the backend cannot answer, the store raises `SessionStoreUnavailable`
(carrying only the driver exception *class name* — never a payload, key or
connection string). Callers treat it as **fail closed**:

| Situation | Behaviour |
| --- | --- |
| resolving a callback reference | refuse: "temporarily unavailable, try again" (the UI does not guess a server) |
| resolving a pending text action | refuse |
| consuming a confirmation | `UNAVAILABLE` → the operation is refused, nothing is executed and nothing is queued for later |
| reading the customer's server list / details | served from PostgreSQL and the provider; Redis is not needed |

There is **no** silent fallback to process-local state in production:
`build_session_store()` accepts `memory` only when `APP_ENV` is
`development`/`test`/`local`/`testing`, and raises otherwise. An in-memory
backend therefore exists for tests and an explicitly marked dev environment,
never as a production degradation path.

After Redis data loss the system remains fail-safe: losing the session state
*cannot* authorise an operation, it can only make an operation unavailable.

---

## 8. Deployment and persistence

`deploy/production/docker-compose.yml`:

* `redis` runs on the private compose network with **no published port**;
* `command: ["redis-server", "--appendonly", "yes"]` with a named volume
  (`redisdata:/data`), so an ordinary restart keeps AOF state;
* a `redis-cli ping` healthcheck gates `api`, `worker` and `bot`;
* `api`, `worker` and `bot` all wait for `migrate` to complete first.

Honest durability statement — Redis is configured, not overclaimed:

| Failure | Consequence |
| --- | --- |
| application/bot restart | references, pending confirmations and prompts survive (AOF + shared store) |
| Redis restart | AOF replay normally restores state; a wiped volume loses it |
| full host failure | Redis state is gone unless the volume is backed up |

In every one of those cases the failure mode is *refusal*, never a double
execution: confirmations are additionally protected because the wallet ledger
and the operation ledger own the exactly-once guarantee, not Redis. Redis only
decides whether a click is allowed to proceed at all.

Transient Telegram state is **not** backed up in production, and it is not a
business record: the durable facts are in PostgreSQL (`renewals`,
`provider_orders`, `wallet_ledger`, `business_log_outbox`, `audit`).

---

## 9. Telegram transport limitation (read this before scaling the bot)

Sharing Redis makes *bot-managed state* safe across replicas. It does **not**
make the Telegram transport safe across replicas.

This deployment uses **long polling**. Two processes polling the same bot token
contend for updates and `getUpdates` conflicts, so:

```text
bot replicas = 1
```

is a requirement, not a preference. It is enforced in
`deploy/production/docker-compose.yml` (`deploy.replicas: 1`) and documented at
the service definition. A rolling deploy briefly overlaps old and new
containers; because state is shared, the new container resolves the buttons the
old one rendered — that is exactly what Redis buys us, and it is why a brief
overlap is tolerable while *permanent* double polling is not.

To scale the bot horizontally, move to a **webhook** first (`api` replicas can
already be scaled freely; the worker likewise, because its financial
concurrency is settled in PostgreSQL).

---

## 10. Operating it

```bash
# Is the shared store actually reachable and round-tripping?
uv run python -m cloud_platform.cli leaseweb doctor

# Inspect namespaced keys (never prints values)
redis-cli --scan --pattern 'cloud-platform:bot:*:v1:*' | head
```

Structured log lines to watch (no payload is ever logged):

| Log | Meaning |
| --- | --- |
| `session store <op> failed: <ExceptionClass>` | Redis unreachable or erroring |
| `session store: malformed JSON payload rejected` | a value we did not write, or corruption |
| `session store: unknown schema version rejected` | a record from a different schema version |
| `confirmation store unavailable; refusing operation` | fail-closed path taken |
| `telegram session store unavailable during callback` | the UI refused the request |

If those appear in production, treat it as an incident: mutations are being
refused (safely, but customers cannot act).

---

## 11. Configuration reference

```toml
[telegram.sessions]
backend = "redis"                  # redis | memory (development/test only)
namespace = "cloud-platform:bot"  # key prefix; change to isolate environments
reference_ttl_seconds = 1800       # how long a button's reference resolves
prompt_ttl_seconds = 900           # how long a text prompt waits for an answer
confirmation_ttl_seconds = 300     # how long a destructive confirmation is valid
replay_ttl_seconds = 1800          # how long a consumed token is remembered

[redis]
url = "redis://redis:6379/0"
```

`backend = "memory"` is refused outside development/test. The doctor command
reports it as a FAIL (`Telegram sessions shared`), so a misconfigured production
deployment is visible before customers hit it.
