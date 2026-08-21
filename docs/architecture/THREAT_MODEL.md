# Threat model starter

## Highest-risk assets
1. Provider API credentials.
2. Wallet/accounting integrity.
3. Payment webhook trust.
4. Ability to create expensive cloud resources.
5. Root/SSH credentials and cloud-init secrets.
6. Admin actions.

## Required controls before public launch
- encrypted provider credentials with key rotation plan
- per-user and global provisioning limits
- wallet reservation + row-level serialization for spend
- payment webhook authenticity + replay protection
- idempotency for every provider/payment mutation
- structured secret redaction
- admin RBAC and audit logging
- per-provider-account concurrency/rate limits
- abuse/blocked-user workflow
- budget/cost circuit breaker when provider spend exceeds thresholds
- automatic reconciliation of orphan/missing provider resources
- backup and restore drills for PostgreSQL

## Abuse model
A public VPS reseller will receive port-scan/spam/phishing/malware complaints. Add a resource-to-user audit mapping, immediate disable/freeze workflow, evidence retention, escalation policy and provider-account blast-radius controls before scaling acquisition.
