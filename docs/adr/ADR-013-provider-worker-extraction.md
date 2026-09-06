# ADR-013: Provider-worker extraction — NOT YET (M16-007)

Status: **decided** — keep provider workers in the monolith image with
separate queue processes; re-evaluate on ops-need.

## Context

Provider I/O (Hetzner `Bearer`, LeaseWeb `X-LSW-Auth`, ArvanCloud plain key)
is already isolated behind the `CloudProvider` port, per-provider throttles,
bounded 429 retries and the ambiguous-mutation probes. The worker image runs
`provisioning` / `billing` / `notify` queues as separate processes
(M16-004) from the SAME immutable image.

## Measured I/O / ops needs

- Provider calls are network-bound with client-side throttles (10 rps) and
  advisory-lock-serialized sync jobs; CPU per worker is low.
- Failure modes (timeouts, 429, 5xx) are handled in-adapter with typed
  errors + retry policies — extraction would add network hops without
  changing the retry budget.
- Ops cost of a second deployable (image, migrations, secrets, runbook)
  exceeds any measured gain at current scale.

## Decision

Do NOT extract a provider-worker service. Scale by running more
`provisioning`-queue worker replicas; keep one image, one migration chain.

## Consequences

- `docker compose --profile workers` (or the VPS `platform.sh` menu) scales
  workers horizontally with no code change.
- Revisit when: provider-call volume needs independent autoscaling, or a
  provider needs a distinct egress/network policy per compliance.
