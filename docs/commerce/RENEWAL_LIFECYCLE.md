# Commercial service lifecycle (renewals, grace, suspension)

How the platform collects money for a prepaid monthly server, and how that is
kept **separate** from what the provider says the machine is doing.

This document covers the commercial half only. The provider integration (43
documented Leaseweb operations) is in
[`../leaseweb/VPS_API_COVERAGE.md`](../leaseweb/VPS_API_COVERAGE.md); the
customer screens are in [`../telegram/SERVER_MANAGEMENT.md`](../telegram/SERVER_MANAGEMENT.md);
the shared transient state is in
[`../telegram/SESSION_STORAGE.md`](../telegram/SESSION_STORAGE.md).

---

## 1. Two state machines, never conflated

```text
INFRASTRUCTURE state                     COMMERCIAL state
(what the provider says)                 (whether the customer paid)
CloudServer.state                        renewals.status
─────────────────────                    ──────────────────────
running                                  active
stopped                                  payment_due
provisioning                             grace_period
error                                    suspended
unknown                                  expired
```

Both are true at once on a real service. `running` + `payment_due` is a normal,
expected combination — the server is up and the customer owes money for the next
period. Nothing in the commercial lifecycle derives its state from a provider
state, and nothing stops a provider server as a *consequence* of a billing
value. The UI renders them as two separate lines:

```text
🟢 وضعیت سرور: روشن            ← provider
💳 وضعیت سرویس: نیازمند پرداخت  ← commercial
📅 مهلت پرداخت: 2026-09-03
🔄 تمدید خودکار: فعال
```

Source of truth for the due instant is the durable commercial row
(`renewals.provider_renewal_at`, written from the provider order/contract facts
at provisioning time, plus `grace_until` for the payable window). It is never
inferred from a Telegram message timestamp, a bot session value or a provider UI
string.

---

## 2. State machine

```text
                     ACTIVE
                       │
        (inside charge window / period ends)
                       ▼
                  PAYMENT_DUE ──── wallet settled ────► ACTIVE (next period)
                       │
              insufficient funds
                       ▼
                 GRACE_PERIOD ──── recharge settles ──► ACTIVE
                       │
               grace expires (policy)
                       ▼
                   SUSPENDED ──── wallet settled ─────► ACTIVE
                       │
              (operator decision, out of band)
                       ▼
        EXPIRED / CANCELLED  — records retained, never deleted
```

`INSUFFICIENT_FUNDS` and `MANUAL_CANCELLATION_REQUIRED` are the older
operator-queue values kept so existing rows stay valid; they behave like
`PAYMENT_DUE` for payability and appear in the operator attention list.

`ATTENTION_STATUSES` = `{INSUFFICIENT_FUNDS, MANUAL_CANCELLATION_REQUIRED,
SUSPENDED}` — the unpaid exposure an operator must look at.
`PAYABLE_STATUSES` = `{PAYMENT_DUE, GRACE_PERIOD, INSUFFICIENT_FUNDS, SUSPENDED}`
— the states in which the customer may still settle the period.

---

## 3. Policy (`[commerce.*]`)

```toml
[commerce.renewal]
enabled = true                     # run automatic wallet collection
charge_before_expiry_hours = 72    # earliest automatic charge attempt
warning_before_expiry_hours = [168, 72, 24]   # customer warnings, per period
grace_period_hours = 48            # payable window after expiry
auto_renew_default = true          # for a NEWLY provisioned service only

[commerce.suspension]
stop_server_after_grace = false    # commercial suspension stays non-destructive
```

Loaded into `CommerceRenewalPolicy` (`modules/renewals/policy.py`). The policy
answers three questions: which warning thresholds are crossed, whether the
charge window is open, and whether suspension may touch infrastructure. No
business policy is hardcoded in a Telegram handler.

---

## 4. One pass of the worker

`RenewalChecker.run()` iterates every non-cancelled record; each service is
checked inside its own guard, so one bad service never stops the batch.

For each service:

1. **Before expiry.** Send every warning threshold the service has reached
   (each exactly once per period — see §6). If automatic renewal is on, the
   charge window is open and the wallet covers the **local** price, settle now.
   Otherwise, if the wallet is short, alert the operator.
2. **At/after expiry.** Move `ACTIVE → PAYMENT_DUE` once and record
   `grace_until` (this is the single "renewal due" event). If auto-renew is on
   and the wallet covers the price, settle.
3. **Inside grace.** Retry the charge on every pass, so a recharge is picked up
   with no customer action. With auto-renew off, the service stays due and the
   customer is told so — never charged.
4. **Grace expired.** `SUSPENDED` (commercial). The provider server is stopped
   **only** if `stop_server_after_grace` is on, and it is never terminated,
   reinstalled or deleted.

---

## 5. Money: exactly-once, database-settled

Every debit reuses the existing wallet hold/capture machinery — the wallet
ledger stays append-only, and balances are never mutated directly.

```text
create_hold(wallet, amount, currency, key)   # idempotent per key
        ↓
capture_hold(wallet, hold, key)              # atomic debit + one CHARGE entry
```

The key is deterministic per (service, period):

```text
renewal-charge:<server_id>:<period-start-date>
```

Consequences, all covered by tests:

* running the worker ten times charges once;
* two workers racing charge once (`create_hold` is unique on the wallet +
  idempotency key, so the loser resolves to the existing hold);
* a crash **after** the capture but **before** the record advanced is detected
  (`get_by_idempotency` returns a `CAPTURED` hold) and never re-charges;
* an under-funded wallet raises `InsufficientHoldBalanceError` — no negative
  balance, no failed ledger write.

**Concurrency is settled in PostgreSQL**, never in Redis: a row lock on the
renewal record plus the wallet hold's unique idempotency key. Redis is not a
financial lock and must never be treated as one.

### Price source

The amount is `record.customer_price_minor` — the **local** price the customer
bought at, captured on the order row. It is never re-derived from a current
provider quote, a cached provider price or a provider API response. Repricing a
customer requires an explicit business decision, not a sync job.

---

## 6. Warnings and notifications (exactly once)

`RenewalKind` (`warn_168h`/`warn_72h`/`warn_24h`, plus legacy `warn_7d/3d/1d`,
`renewal_due`, `grace_started`, `grace_expired`, `suspended`, `admin_insufficient`,
`manual_cancellation`, `charged`) is deduplicated per `(server, kind, period)` in
the durable `renewal_notifications` table — a unique constraint, not a Redis TTL.
So a repeated worker pass, a restart or a second worker cannot send the same
warning twice, and the guarantee survives a Redis wipe.

Delivery never blocks financial processing:

```text
wallet settlement / state transition
        ↓
durable notification log + business-log outbox event
        ↓
asynchronous delivery (Telegram)
```

A Telegram delivery failure cannot roll back a settlement, a renewal or a state
transition.

---

## 7. Insufficient funds, grace and recharge

```text
ACTIVE ──► PAYMENT_DUE ──► GRACE_PERIOD ──► SUSPENDED
              │                 │
              └── recharge ─────┴──► ACTIVE
```

* Nothing is destroyed on a shortfall; the VPS keeps running.
* `grace_until` is durable, so the customer's deadline does not move when the
  provider's date drifts.
* `🖥 سرورهای من` shows `💳 وضعیت سرویس: نیازمند پرداخت` and
  `📅 مهلت پرداخت: …` plus a `💳 تمدید اکنون` button.
* Recharge reuses the existing wallet top-up flow (`modules/payments`); there is
  no second payment implementation. After a successful recharge the next worker
  pass settles the period on its own — or the customer taps renew-now.

---

## 8. Manual renewal ("renew now")

`🖥 سرورهای من → ⚙️ مدیریت → 💳 تمدید اکنون`.

It calls `ServerManagementService.renew_now()`, which:

1. reloads the local row and proves ownership (a foreign server looks missing);
2. checks the `billing` policy group and the local lifecycle state;
3. **consumes a one-time confirmation token** (money is being moved);
4. calls `RenewalChecker.settle_now()`, which is the *same* idempotent
   settlement the worker uses — same price, same idempotency key.

Outcomes are honest and machine-coded:

| `reason` | Meaning | Customer message |
| --- | --- | --- |
| `charged` | this call settled the period | ✅ renewed, amount + new period |
| `already_charged` | the period was already paid for | ℹ️ already processed, not charged again |
| `insufficient_funds` | the wallet is short | ⚠️ top up your wallet; the service stays active |
| `not_payable` | nothing is due | ℹ️ nothing is due for renewal |
| `manual_review_required` | more than one period behind | 🔔 needs support review |
| `no_renewal_record` / `no_due_date` | the platform cannot bill it | ℹ️ contact support |

**Early renewal is deliberately not offered.** Only a due/grace/suspended
period can be settled; an active period that has not ended is `not_payable`.
Implementing early renewal would need a proration/length policy that the product
has not defined, so it is documented as a limitation rather than guessed at.

No provider API is called anywhere on this path: Leaseweb renews **its own
contract with the platform** on its own billing cycle, and the modern VPS API
exposes no renewal operation to call (§26 below).

---

## 9. Automatic renewal (durable preference)

`🔄 تمدید خودکار` in the manage menu, stored on `renewals.auto_charge_enabled`
(a real column, default `true` for newly provisioned services). Toggling it:

* proves ownership;
* is checked against the `billing` policy group;
* is audited (`service.auto_renew_changed`) and emits
  `service.auto_renew_enabled` / `service.auto_renew_disabled`;
* reports the **stored** value, so the screen can never show a value the
  database does not hold;
* is never kept only in Redis.

`auto_renew_default` decides the initial value for a new service. Existing rows
keep their own stored setting, so changing the default never flips a customer's
preference.

---

## 10. Suspension and expiry

* **Suspension is commercial.** It never terminates, reinstalls or deletes a
  provider server. `stop_server_after_grace` defaults to `false`; turning it on
  is an explicit, configurable, audited operator decision. Every suspension
  writes an audit entry naming the reason, the balance and the policy in force.
* **Expiry ≠ provider cancellation.** `EXPIRED` marks a local service whose
  period is over while the financial and service records are retained. The
  customer's read access, recharge path and history stay available; mutations
  that the policy no longer allows are refused.
* **Termination is out of scope.** No code path stops-and-calls-it-cancelled.
  When a service genuinely needs terminating at the provider, the operator
  runbook owns it; the platform records the need (attention state) instead of
  inventing a provider call.

---

## 11. Business events

Emitted through the durable business-log outbox (`emit_safe`, so a broken sink
never breaks a settlement), from two places:

**The worker** (`RenewalChecker`) emits on the same exactly-once gate as the
customer notification (`renewal_notifications` is unique per server/kind/period):

| Event | Emitted when |
| --- | --- |
| `service.renewal_warning` | a configured warning threshold is crossed |
| `service.renewal_due` | `ACTIVE → PAYMENT_DUE` (which is also the moment the payable window opens) |
| `service.renewal_succeeded` | a period was settled automatically |
| `service.renewal_failed_insufficient_balance` | an automatic settlement could not be funded |
| `service.grace_expired` | grace elapsed and the service was marked suspended |
| `service.suspended` | the service moved to `SUSPENDED` |
| `service.operator_attention_required` | the operator must resolve it (e.g. manual cancellation) |

The `RenewalKind` → event mapping is one table (`_KIND_EVENT`), so a kind that
exists but is not yet emitted (e.g. `GRACE_STARTED`, which the due event already
covers) is visible as such rather than silently absent.

**The customer actions** (`ServerManagementService`) emit when a human moves the
service:

| Event | Emitted when |
| --- | --- |
| `service.renewal_succeeded` | a manual renew-now settled the period (or found it already settled) |
| `service.renewal_failed_insufficient_balance` | renew-now could not be funded |
| `service.renewal_due` | renew-now found nothing payable |
| `service.operator_attention_required` | renew-now found the service more than one period behind |
| `service.auto_renew_enabled` / `service.auto_renew_disabled` | the preference changed |

The builders (`modules/businesslog/events.py`) have **no parameter** for a
wallet secret, credential, console URL or provider key, so those physically
cannot be logged. Event keys are deterministic in the local identity plus the
period/outcome, so a repeated pass or a double tap posts one card.

---

## 12. Operator visibility

```bash
uv run python -m cloud_platform.cli renewals list
uv run python -m cloud_platform.cli renewals check
```

Structured metrics/log counters on the same names as the events
(`renewal_due`, `renewal_charged`, `renewal_suspended`, …) plus the audit trail.
`renewals list` shows the commercial status, the auto-renew flag and whether the
due date is estimated.

---

## 13. Limitations (explicit, not accidental)

1. **No provider renewal/cancellation call.** The modern Leaseweb VPS API
   documents neither, so none is invented. Recharge → wallet rent is local.
2. **No early renewal** — see §8.
3. **No proration** for a mid-period change; the platform sells whole periods.
4. **A single manual settlement clears one period.** A service several periods
   behind is routed to the operator (`manual_review_required`) rather than
   silently taking money for one month of several.
5. **Transient UI state for renew-now is Redis**; the money and the state
   transition are not. Losing Redis can only refuse a click.
