#!/usr/bin/env bash
# Cloud Server Platform — bash ops menu (run on the VPS after install.sh).
#
#   cd /opt/cloud-platform && ./platform.sh
#
# Wraps the everyday operator tasks so selling Hetzner/LeaseWeb servers never
# needs raw docker commands: status, logs, update, restart, catalog sync,
# backups, secret wizard, smoke tests, full teardown.
set -euo pipefail

cd "$(dirname "$0")"

compose() { docker compose "$@"; }

need_env() {
  if [ ! -f .env ]; then
    echo ".env not found — copying from .env.example; run option 7 (secrets) next." >&2
    cp .env.example .env
    chmod 600 .env
  fi
}

health() {
  echo "— containers —"
  compose ps
  echo "— api —"
  curl -fsS http://127.0.0.1:8000/health/live && echo || echo "api DOWN"
  curl -fsS http://127.0.0.1:8000/health/ready && echo || echo "readiness NOT OK"
}

logs_menu() {
  echo "service (api/worker/bot/postgres/redis/all):"
  read -r svc
  svc=${svc:-all}
  if [ "$svc" = all ]; then compose logs --tail=100 -f
  else compose logs --tail=200 -f "$svc"; fi
}

pricebook_menu() {
  echo "Publish a price-book version (charged prices derive from it)."
  printf 'reason (required): '; read -r reason
  [ -n "$reason" ] || { echo "reason is required."; return 0; }
  printf 'admin telegram id: '; read -r admin_id
  printf 'rules file (empty = single wildcard rule): '; read -r rules
  if [ -n "$rules" ]; then
    compose run --rm api python scripts/publish_price_book.py --rules-file "$rules" --reason "$reason" --admin-telegram-id "$admin_id"
  else
    printf 'margin factor [1.20]: '; read -r factor
    compose run --rm api python scripts/publish_price_book.py --margin-factor "${factor:-1.20}" --reason "$reason" --admin-telegram-id "$admin_id" --bootstrap-admin
  fi
}

sync_menu() {
  echo "provider (hetzner/leaseweb/arvancloud/all):"
  read -r p
  p=${p:-all}
  if [ "$p" = all ]; then compose run --rm api python scripts/sync_catalog.py
  else compose run --rm api python scripts/sync_catalog.py --provider "$p"; fi
}

secrets_menu() {
  need_env
  echo "Leave empty to keep the current value. Secrets are not echoed."
  set_key() {
    local key="$1" val
    printf '%s: ' "$key"
    IFS= read -rs val || true; echo
    [ -n "$val" ] || return 0
    local esc
    esc=$(printf '%s' "$val" | sed 's/[\/&]/\\&/g')
    if grep -qE "^${key}=" .env; then sed -i "s/^${key}=.*/${key}=${esc}/" .env
    else printf '%s=%s\n' "$key" "$val" >> .env; fi
  }
  set_key TELEGRAM_BOT_TOKEN
  set_key CALLBACK_SIGNING_KEY
  set_key HETZNER_API_TOKEN
  set_key LEASEWEB_API_KEY
  set_key ZARINPAL_MERCHANT_ID
  set_key PROVIDER_CREDENTIAL_ENCRYPTION_KEY
  chmod 600 .env
  echo "saved."
}

backup_menu() {
  echo "running one encrypted backup…"
  compose --profile backup run --rm backup
  echo "backups on the volume:"
  docker volume inspect cloud-platform_backups >/dev/null 2>&1 || true
  compose run --rm api python -m cloud_platform.backup --help 2>/dev/null || true
}

update_menu() {
  echo "pull + rebuild + migrate + restart (safe order)…"
  git pull --ff-only
  compose build
  compose run --rm api migrate
  compose up -d api
  compose stop worker bot 2>/dev/null || true
  compose up -d worker bot 2>/dev/null || true
  echo "updated."
}

smoke_menu() {
  echo "post-deploy smoke…"
  uv run python scripts/post_deploy_smoke.py 2>/dev/null || python3 scripts/post_deploy_smoke.py || curl -fsS http://127.0.0.1:8000/health/live
}

teardown_menu() {
  echo "TYPE 'yes' to stop everything AND DELETE volumes (data loss):"
  read -r confirm
  [ "$confirm" = yes ] || { echo "aborted."; return 0; }
  compose down -v
}

show_menu() {
  cat <<'EOF'

  ===== Cloud Platform =====
  1) status (containers + health)
  2) start all (postgres/redis/api/worker/bot)
  3) stop all
  4) restart api/worker/bot
  5) logs
  6) sync catalog (hetzner/leaseweb)
  12) publish price book (margins)
  7) secrets wizard (.env)
  8) backup now
  9) update (git pull + migrate + restart)
  10) smoke test
  11) full teardown (DELETE data)
  0) exit
EOF
  printf 'choice: '
}

main() {
  need_env
  while true; do
    show_menu
    read -r choice || break
    case "$choice" in
      1) health ;;
      2) compose up -d postgres redis && sleep 2 && compose run --rm api migrate && compose up -d api worker bot ;;
      3) compose stop ;;
      4) compose up -d api && compose restart worker bot 2>/dev/null || compose up -d worker bot ;;
      5) logs_menu ;;
      6) sync_menu ;;
      12) pricebook_menu ;;
      7) secrets_menu ;;
      8) backup_menu ;;
      9) update_menu ;;
      10) smoke_menu ;;
      11) teardown_menu ;;
      0) break ;;
      *) echo "unknown choice" ;;
    esac
    echo
  done
}

main "$@"
