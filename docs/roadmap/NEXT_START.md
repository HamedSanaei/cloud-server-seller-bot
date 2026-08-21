# Recommended first execution wave

When you give this repository to Codex for the first real implementation session, start with M00/M01 rather than building Telegram screens immediately.

Suggested parallel wave:

- **Supervisor:** M00-001 + M00-005, then own M01 migration/schema integration.
- **Hetzner sub-agent 1:** M00-002 (lockfile) then M01-006 (outbox repository) after schema contract exists.
- **Hetzner sub-agent 2:** M00-004 (CI) + M01-007 (logging/redaction), staying out of schema files.

Do not begin public provisioning until wallet holds, billing snapshots, idempotent operations and reconciliation are implemented. A pretty bot before these controls creates financial risk.
