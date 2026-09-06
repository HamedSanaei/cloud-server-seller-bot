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
