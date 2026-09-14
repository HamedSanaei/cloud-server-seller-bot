# Telegram server management («سرورهای من»)

The customer-facing VPS management experience. It exposes the *safe subset* of
the modern Leaseweb VPS integration (`docs/leaseweb/VPS_API_COVERAGE.md`) from
Telegram, through an application service that owns ownership, policy,
confirmation, idempotency, audit and business events.

The provider layer is untouched by this feature: Telegram never sees a
Leaseweb URL, a provider resource id, an API key or a credential. Everything
flows

```
Telegram  ->  ServerManagementUi (renderer)
          ->  ServerManagementService (ownership, policy, confirmation, audit)
          ->  providers.vps_ports (provider-neutral capabilities)
          ->  Leaseweb adapter -> Leaseweb API
```

## Customer flow

```
Main menu
  └─ 🖥 سرورهای من              (servers.list:{page})
       ├─ ⚙️ مدیریت {n}          (servers.manage:{ref})
       │    ├─ 📋 مشخصات        (servers.view:{ref})
       │    ├─ ⏹ خاموش کردن / ▶️ روشن کردن / 🔄 ریبوت
       │    ├─ 🖥 کنسول
       │    ├─ 📊 مصرف ترافیک
       │    ├─ 📸 Snapshot  -> 📋 لیست / ➕ ساخت / ♻️ بازیابی / 🗑 حذف
       │    ├─ 💿 نصب مجدد  -> انتخاب سیستم‌عامل -> تأیید
       │    ├─ 🔑 بازنشانی رمز -> تأیید
       │    ├─ 🌐 مدیریت IP  -> 🔤 Reverse DNS / 🚫 Null route / ✅ برداشتن
       │    ├─ 📀 ISO        (off by default)
       │    ├─ 📡 مانیتورینگ
       │    ├─ ✏️ نام سرور
       │    ├─ 💳 تمدید اکنون      (only while the period is payable) -> تأیید
       │    └─ 🔄 تمدید خودکار    (frozen per deployment by `billing`)
       └─ ↩️ بازگشت
  └─ 🔄 بروزرسانی (inside details)
```

The details screen shows **two independent facts** side by side: the provider's
infrastructure state («🟢 وضعیت سرور: روشن») and the customer's commercial state
(«💳 وضعیت سرویس: فعال / نیازمند پرداخت», «📅 اعتبار تا», «📅 مهلت پرداخت»,
«🔄 تمدید خودکار»). They are never derived from each other — a running server
can owe money. See [`../commerce/RENEWAL_LIFECYCLE.md`](../commerce/RENEWAL_LIFECYCLE.md)
for how those commercial states are reached, and
[`SESSION_STORAGE.md`](SESSION_STORAGE.md) for where the buttons' state lives.

Server list rows show only customer-friendly facts: location flag + city, the
main IP (or «IP هنوز اختصاص نیافته» while provisioning), the OS and the state.
Each row has its own «⚙️ مدیریت» button; the list is paginated
(`⬅️ قبلی  1 / 4  بعدی ➡️`) with a stable newest-first ordering.

## Capability matrix

| Operation            | Customer | Confirmation | Notes |
| -------------------- | -------- | ------------ | ----- |
| View server          | yes      | no           | IP, OS, location, plan, renewal |
| Refresh (reconcile)  | yes      | no           | **read-only**: GETs only, never a mutation |
| Start                | yes      | no           | hidden while already running |
| Stop                 | yes      | **yes**      | loses access until started again |
| Reboot               | yes      | no           | hidden unless the server is running |
| Console              | yes      | no           | temporary URL, sent as a one-tap link |
| Traffic usage        | yes      | no           | provider-reported directions only |
| List snapshots       | yes      | no           | |
| Create snapshot      | yes      | **yes**      | name generated server-side |
| Restore snapshot     | yes      | **yes**      | destructive |
| Delete snapshot      | yes      | **yes**      | destructive |
| Reinstall OS         | yes      | **yes**      | image list fetched live from the provider |
| Reset password       | yes      | **yes**      | the provider returns no password; none is invented |
| List IPs             | yes      | no           | main IP, null-route state, reverse DNS |
| Set reverse DNS      | yes      | no           | value typed by the customer, validated |
| Null route an IP     | yes      | **yes**      | destructive |
| Remove a null route  | yes      | no           | reversible |
| Attach / detach ISO  | policy   | **yes**      | `iso = false` by default |
| Monitoring status    | yes      | no           | |
| Enable monitoring    | yes      | no           | reversible |
| Rename server        | yes      | no           | ≤ 64 chars, letters/digits/`-_. ` |
| Renew now            | policy   | **yes**      | settles ONE local period from the wallet; never a provider call. `billing = true` by default |
| Auto-renew toggle    | policy   | no           | durable column, audited; never Redis-only |
| Credential CRUD      | **no**   | –            | operator-only (`OPERATOR_ONLY_OPERATIONS`) |
| ISO catalogue        | yes      | –            | only reached through the attach flow |
| Cancel / terminate   | **no**   | –            | see «Not implemented» below |

## Authorization model

The application service is the only place that can turn an intent into provider
work, and it checks, in order:

1. **Feature** — `[features.server_management] enabled`.
2. **Ownership** — the local row is re-loaded and `user_id` compared. A missing
   server and another customer's server are indistinguishable
   (`ServerNotFoundError`, one uniform "server not found" screen), so the bot
   cannot be used to enumerate servers.
3. **Policy** — the capability group is exposed *and* the provider adapter
   actually implements it (`VpsCapabilities`, detected structurally, so no code
   compares a provider name).
4. **State** — the local lifecycle state allows the operation (start only when
   stopped, stop/reboot only when running).
5. **Resource** — the server has a provider resource id.
6. **Arguments** — an IP must appear in the provider's CURRENT IP list for this
   VPS before it can be touched.

Callbacks are signed (HMAC, `v1|<flow>:<screen>:<args>|<sig>`) and are never
trusted: they carry an opaque 8-character **reference** minted per
(customer, server) by the bot session store, never a UUID, a provider id or a
confirmation token. A forged or foreign reference resolves to nothing and the
customer sees the stale-button screen — before any service call.

## Callback budget

Telegram rejects `callback_data` longer than **64 bytes**, and a signed callback
already spends 21 on `v1|` + `|` + the 16-character HMAC. A bare
`servers:view:<uuid>` would be 71 bytes. So buttons carry:

- a **server reference** (8 chars) instead of a UUID;
- a **deterministic nonce** (10 hex chars, derived from the operation and its
  arguments) instead of a confirmation token;
- an **index** into a list the customer actually saw (images, snapshots, ISOs,
  IPs), remembered in the session, instead of a provider identifier.

A test sweeps every rendered screen and asserts each button fits.

## Confirmation model

Destructive operations require a **one-time confirmation token** minted by the
application service and bound to

```
(customer, server, operation, digest(arguments), expiry, nonce)
```

The token never travels through Telegram: the confirmation screen stashes it in
the bot session behind a deterministic nonce and the button carries only that
nonce. Consequences:

- **Double tap** on «✅ انجام بده» → the nonce resolves once; the second tap
  finds nothing and answers «این عملیات همین حالا در حال اجراست؛ دوباره ارسال
  نشد». Provider mutation count = 1.
- **Replay** of a consumed token → `REPLAYED` (never executed twice).
- **Expired** token → «زمان تأیید این عملیات به پایان رسیده است» (TTL from
  `confirmation_ttl_seconds`, default 15 minutes).
- **Modified arguments** (a different image, snapshot or IP than the one shown)
  → `MISMATCH`; nothing runs.
- **Wrong customer or server** → uniform not-found / mismatch; nothing runs.

## Ambiguous provider outcomes

A read/write timeout, a dropped connection or a 5xx *after* the request was
transmitted means the provider outcome cannot be proven. The platform never
re-sends such a request:

- the power path records the operation as an unprovable outcome in the ledger
  (`Operation.provider_response = {"outcome": "unknown"}`) and raises
  `PowerOutcomeUnknownError`;
- the service records `server.<operation>_outcome_unknown` in the audit trail
  and emits a `server.operation_failed` business event with category
  `outcome_unknown`;
- the customer sees «🟠 نتیجه عملیات هنوز مشخص نیست… درخواست مجدد ارسال نشد» —
  never "failed" and never "done";
- reconciliation (the «🔄 بروزرسانی» button or the background reconciler)
  resolves it with **reads only**.

## Business logging

Mutations emit durable events through the existing business-log outbox
(`server.started`, `server.stopped`, `server.reboot_requested`,
`server.console_requested`, `server.reinstall_requested`,
`server.snapshot_created`, `server.snapshot_restored`, `server.snapshot_deleted`,
`server.password_reset_requested`, `server.ip_null_routed`,
`server.ip_unnull_routed`, `server.operation_failed`). Read-only page views are
deliberately **not** logged, and a broken sink can never block an action
(`emit_safe`).

Safe metadata only: the platform user, the *local* server id, the provider key,
the state, the operation, the result and a timestamp. IPs are masked
(`88.1.2.0`). Never logged: API keys, credentials, passwords, console URLs or
tokens, SSH private keys, `Authorization` / `X-LSW-Auth` headers.

## Configuration

```toml
[features.server_management]
enabled = true
manager = true
power = true
console = true
traffic = true
snapshots = true
reinstall = true
password_reset = true
iso = false                        # advanced, off by default
ip_management = true
monitoring = true
billing = true                     # renew-now + auto-renew toggle (local, no provider call)
page_size = 5
traffic_window_days = 30
confirmation_ttl_seconds = 900
```

No secrets live in this section. Set `enabled = false` to remove the whole
experience («این بخش فعلاً برای شما فعال نیست»).

## Operator-only surface

Everything below stays out of the customer UI regardless of configuration and is
reachable only through the operator diagnostics/CLI or an explicit admin flow:

- credential creation, update and deletion (`storeCredential1`,
  `updateCredential1`, `deleteCredential1`, `deleteCredentials1`);
- direct ISO catalogue listing outside the attach flow;
- any raw provider field, error body, stack trace or internal identifier.

## Known limitations

- **No cancellation/termination.** The modern VPS API inventory contains no
  cancellation operation, so none is exposed. Termination must go through the
  commercial lifecycle (renewal/abuse flow) once the Contracts API is wired.
- **No customer-facing credential display.** Leaseweb's credential endpoints are
  operator-only by policy; a password reset therefore reports the request, not a
  password, because the provider response carries none.
- **Console link expiry** is whatever the provider returns; the screen says the
  link is temporary and must not be shared.
- **Single-process sessions.** Server references and pending confirmations live
  in the bot process. Run one polling process (the supported deployment); a
  multi-process bot would need a shared `ConfirmationStore`/session store.
