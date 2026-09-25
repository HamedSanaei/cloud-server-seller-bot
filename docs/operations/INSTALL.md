# Install & operate (single VPS)

One-line install on a clean Ubuntu 22.04/24.04 VPS (as root):

```bash
curl -fsSL https://raw.githubusercontent.com/<org>/cloud-server-platform-starter/main/install.sh | sudo bash
```

Non-interactive (tokens via environment):

```bash
curl -fsSL .../install.sh -o install.sh && chmod +x install.sh
sudo TELEGRAM_BOT_TOKEN=... HETZNER_API_TOKEN=... LEASEWEB_API_KEY=... ./install.sh --yes
```

What the installer does (idempotent, safe to re-run):

1. Installs docker + uv + python if missing.
2. Clones/updates the repo to `/opt/cloud-platform` (`--dir` to change).
3. Creates `.env` from `.env.example` (never overwrites) and fills secrets
   (auto-generates `CALLBACK_SIGNING_KEY` + `PROVIDER_CREDENTIAL_ENCRYPTION_KEY`).
4. `docker compose build`, starts postgres/redis, runs `migrate`.
5. Syncs catalogs (`scripts/sync_catalog.py --provider hetzner|leaseweb`).
6. Starts `api + worker + bot` and curls `/health/live`.

## Daily ops: the bash menu

```bash
cd /opt/cloud-platform && ./platform.sh
```

| # | Action |
| --- | --- |
| 1 | status (containers + `/health/live` + `/health/ready`) |
| 2 | start all (postgres/redis → migrate → api/worker/bot) |
| 3 | stop all |
| 4 | restart api/worker/bot |
| 5 | logs (per service, follow) |
| 6 | sync catalog (hetzner / leaseweb / arvancloud / all) |
| 7 | secrets wizard (Telegram, Hetzner, LeaseWeb, ZarinPal — never echoed) |
| 8 | backup now (encrypted pg_dump on the `backups` volume) |
| 9 | update (git pull + rebuild + migrate + safe restart) |
| 10 | smoke test (`scripts/post_deploy_smoke.py`) |
| 11 | full teardown (asks for `yes`, deletes volumes) |

## Selling checklist (after install)

1. `./platform.sh` → 7: set `TELEGRAM_BOT_TOKEN`, `HETZNER_API_TOKEN`,
   `LEASEWEB_API_KEY`, `ZARINPAL_MERCHANT_ID`.
2. Option 6: sync catalogs.
3. Publish a price book version + margins (see `docs/operations/RUNBOOK.md`).
4. Enable the offers you want to sell (catalog visibility).
5. Open the bot → `/menu` → buy a server; top up via ZarinPal.
6. `curl -fsS http://127.0.0.1:8000/health/ready` must be `ok`.

Production notes: `.env` is `chmod 600`; provider keys live in
`CredentialHolder` at runtime and rotate without downtime (M10-008);
backups are encrypted (`BACKUP_ENCRYPTION_KEY`); never commit `.env`.

## Configuration sources (TOML first)

The installer keeps generating `.env` for backward compatibility, and that
still works because environment values win over the TOML file. The supported
production model, however, is a single operator-managed TOML file:

```bash
sudo install -d -m 0750 -o root -g 999 /etc/cloud-server-seller
sudo install -m 0640 -o root -g 999 \
    configuration.example.toml \
    /etc/cloud-server-seller/configuration.toml
sudo "$EDITOR" /etc/cloud-server-seller/configuration.toml    # fill in secrets
export CLOUD_PLATFORM_CONFIG_FILE=/etc/cloud-server-seller/configuration.toml
cd deploy/production && docker compose up -d
```

`configuration.example.toml` in the repository ROOT is the single canonical
template (there is no second, production-flavoured copy). Keep the real
`configuration.toml` locally next to it — git-ignored, never committed — merge
new keys from the template before you upload it to the server path above.

`deploy/production/docker-compose.yml` mounts the file read-only into every
container and sets only that one non-secret bootstrap variable — no provider
key, bot token, payment secret or encryption key is placed in the environment.

Lookup order for the file: `CLOUD_PLATFORM_CONFIG_FILE` → `./configuration.toml`
→ `/etc/cloud-server-seller/configuration.toml`. Value precedence is
**explicit arguments → environment → TOML → `.env` → defaults**.
Reload after an edit: `docker compose restart api worker bot` (no rebuild).

Pre-flight checks (read-only, secrets are never printed):

```bash
uv run python -c "from cloud_platform.core.config import get_settings as g; s=g(); print(s.app_env, s.database_url.split('@')[-1], bool(s.telegram_bot_token))"
uv run python -m cloud_platform.cli leaseweb doctor
```
