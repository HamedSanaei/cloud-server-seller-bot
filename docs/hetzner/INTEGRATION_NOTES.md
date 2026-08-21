# Hetzner integration notes

These are durable integration assumptions for the first provider adapter.

- Cloud mutations may be asynchronous actions; workers should wait/reconcile rather than assume the HTTP response means final state.
- API requests are rate limited per project and adapters should capture limit/remaining/reset headers.
- Catalog is dynamic. Locations, server types, images and prices must be synchronized rather than hard-coded.
- Server billing continues while a server exists even if powered off; deletion is the stop-cost operation.
- Partial hourly usage can be rounded up by the provider, so provider-cost policy must not be inferred from UI timestamps alone.
- Store provider resource IDs and platform-owned labels/correlation metadata.
- Treat 404 on delete reconciliation as success, not an error that causes infinite retry.
- Treat timeouts after mutation as ambiguous until reconciled.

Before production, add contract tests against a dedicated low-risk Hetzner project and enforce strict resource cleanup.
