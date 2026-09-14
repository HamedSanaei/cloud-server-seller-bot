# ADR-014: One runtime configuration file (`configuration.toml`)

Status: **decided** — `configuration.toml` is the runtime source of truth;
environment variables remain a bootstrap/test compatibility layer.

## Context

Runtime configuration was spread across `.env`, environment variables and
field defaults. That made a production install hard to inspect: which value
actually took effect could only be answered by printing the process
environment, and provider secrets leaked into `docker inspect` output.

## Decision

1. **One operator-managed file per environment**, looked up in this order:
   1. an explicit path passed to `load_settings(path)` (tests),
   2. `$CLOUD_PLATFORM_CONFIG_FILE` — bootstrap only, never a secret; a set
      but missing file is a hard error,
   3. `./configuration.toml` (development),
   4. `/etc/cloud-server-seller/configuration.toml` (production default).
2. **Value precedence**: explicit constructor args > environment variables >
   `configuration.toml` > `.env` > field defaults.
3. **Nested TOML maps onto the existing flat `Settings` contract**
   (`[providers.leaseweb].api_key` → `leaseweb_api_key`), so no call site
   changes. `[providers.<key>]` is handled generically: adding a provider
   section is configuration, not code.
4. **A bare `Settings()` never reads the filesystem.** Only
   `load_settings()` / `get_settings()` / `Settings.model_validate_toml()`
   select an active file. Without this, a developer's local
   `configuration.toml` silently changes unit-test results.
5. **`tomllib`** (stdlib, Python ≥ 3.11) parses the file; no new dependency.
6. `.env` stays supported because the test suite and the existing installer
   depend on it. Since environment values win, an existing deployment keeps
   working unchanged while the TOML file is introduced.
7. `configuration.example.toml` (committed) documents every section with fake
   values; the real `configuration.toml` is git-ignored.

## Consequences

- Production containers mount the file read-only and export exactly one
  non-secret variable; `docker inspect` no longer reveals provider keys.
- `alembic/env.py` falls back to `Settings.database_url` so `migrate` works
  with a TOML-only deployment.
- A malformed file fails fast (`ConfigFileError`) instead of silently running
  on defaults.
- Changing configuration is a file edit + service restart, never a rebuild.
