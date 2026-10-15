# Configuration-managed Telegram Super Admins

`telegram.admin_chat_id` is only a notification destination. It is never an
administrator identity, even when it happens to be a positive private-chat ID.
No hardcoded Telegram account is privileged. The inspected operator-local file
had a notification destination but no dedicated administrator-identity key;
the application must not guess which person should administer it.

Operators explicitly list verified **Telegram User IDs**, not usernames,
channel/group IDs or forwarded-message senders:

```toml
[telegram]
superadmin_user_ids = [123456789, 987654321] # examples only: replace both
```

The canonical example defaults to an empty array: management access is denied.
Values must be positive integer PostgreSQL BIGINTs, not strings, floats or
booleans. Duplicate IDs are canonicalized. Notification configuration stays
independent. Real operator IDs and credentials must never enter this public
repository. Updating the production configuration, applying migration `0055`,
and deployment require separate operator approval; this change does none of
those things automatically from a feature branch or pull request.

## Reconciliation and removal

API, bot and worker startup each reconcile the same server-owned configuration
before serving application traffic/polling/jobs. The existing `admin` RBAC role
represents Super Admin privileges; there is no competing permission system.
Authorization requires the configured Telegram identity **and** the existing
persisted active role and RBAC permissions. REST/web additionally require their
existing bearer/session token scopes. A legacy manually assigned `admin` role
without configured identity is insufficient for management access.

An additive `telegram_superadmin_assignments` table records desired policy,
including IDs whose users have not registered. Reconciliation and authenticated
onboarding share a transaction-scoped PostgreSQL advisory lock. Existing users
are updated in place; grants set `role=admin` and `status=active`. Startup repairs
those configured privileges. Grants, activations and configuration changes have
immutable SYSTEM audit events in the same transaction. Unchanged or concurrent
identical reconciliations do not duplicate events. Audit failure rolls back the
privilege change.

Removed IDs are disabled and config-managed bound admin roles become `user`.
Account status is not reset on removal. Assignment rows and immutable audit
history remain. Unrelated accounts are not demoted. An empty list revokes all
configuration-managed assignments. Every process must restart against the same
updated file; running different configuration versions concurrently is an
operator error, and serialized startup reconciliation uses the last completed
configuration. Stale onboarding cannot regrant a removed assignment. Privileged
wallet, gateway and user-status mutations validate the current enabled binding
and persisted actor inside the transaction under the same policy lock, so a
cached actor/config cannot bypass a completed revocation.

## First registration and trusted identity

Only private, human Telegram bot updates whose sender ID equals the private
chat ID reach onboarding. Bot, anonymous/sender-chat, group, cross-chat and
untrusted callback contexts are rejected. Username/email are never account-link
proofs. Users, pending grants, wallets and grant audits commit atomically;
concurrent `/start` calls resolve one existing UUID and one wallet. Existing
wallets use `ON CONFLICT DO NOTHING`: balance, currency, ledger, payment history
and existing identity data are never reset. A username collision creates a
separate alias rather than taking over another account.

A pending enabled assignment is granted when that identity first registers.
Ordinary subsequent requests do **not** undo an administrator's runtime
freeze/ban/role change; an explicit subsequent startup reconciliation is what
reapplies the configured active role. Public APIs expose no Telegram identity
registration/linking or role-grant payload. Forged headers/payloads do not call
the trusted onboarding operation; production header identity remains disabled.

## Management and manual credit

After an approved configuration update/deployment, `/start` from an authorized
private account shows a persistent **Management** button. Management includes
gateway settings and **Manual Customer Wallet Credit**. Unconfigured/inactive
users do not get this keyboard; authorization is still enforced on every
command/callback and in application/database services, not just by visibility.

Credit keeps customer selection, currency-bound amount, mandatory reason,
signed confirmation, actor-bound TTL state, cancellation and stable idempotency
keys. Duplicate confirmations replay the same immutable ledger fact. Balance,
ledger, audit and enabled business-event outbox commit together. Confirmation
currency is checked again under the wallet lock. Direct `/admin_credit` no
longer posts money without confirmation: it directs the operator to Management.
Super Admin self-top-ups remain forbidden. Returning to the storefront restores
its persistent keyboard.
