#!/usr/bin/env bash
# Cloud Server Platform — one-line installer for a clean Ubuntu VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/<org>/cloud-server-platform-starter/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --yes --providers hetzner,leaseweb
#
# What it does (idempotent — safe to re-run):
#   1. installs docker + uv + python (if missing)
#   2. clones/updates the repo into $INSTALL_DIR (default /opt/cloud-platform)
#   3. creates .env from .env.example (never overwrites an existing .env)
#   4. interactively (or via env/flags) collects secrets into .env
#   5. builds images, runs migrations, syncs catalogs, starts api/worker/bot
#   6. prints the Telegram + API next steps
#
# Non-interactive mode: set env vars or pass flags (see --help).
set -euo pipefail

REPO_URL_DEFAULT="https://github.com/<org>/cloud-server-platform-starter.git"
BRANCH_DEFAULT="main"
INSTALL_DIR_DEFAULT="/opt/cloud-platform"
PROVIDERS_DEFAULT="hetzner,leaseweb"

REPO_URL="${REPO_URL:-$REPO_URL_DEFAULT}"
BRANCH="${BRANCH:-$BRANCH_DEFAULT}"
INSTALL_DIR="${INSTALL_DIR:-$INSTALL_DIR_DEFAULT}"
PROVIDERS="${PROVIDERS:-$PROVIDERS_DEFAULT}"
YES=0
WITH_BOT=1
DO_SYNC=1
DO_UP=1

usage() {
  sed -n '2,20p' "$0"
  echo "Flags: --yes --dir PATH --repo URL --branch B --providers hetzner,leaseweb --no-bot --no-sync --no-up --help"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1; shift ;;
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --providers) PROVIDERS="$2"; shift 2 ;;
    --no-bot) WITH_BOT=0; shift ;;
    --no-sync) DO_SYNC=0; shift ;;
    --no-up) DO_UP=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown flag: $1" >&2; usage >&2; exit 1 ;;
  esac
done

log() { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "run as root (or with sudo): curl ... | sudo bash"
  fi
}

install_prereqs() {
  log "installing prerequisites (docker, git, curl, python)…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y git curl ca-certificates python3 python3-venv openssl
  if ! command -v docker >/dev/null 2>&1; then
    log "installing docker…"
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
  fi
  if ! command -v uv >/dev/null 2>&1; then
    log "installing uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    ln -sf "$HOME/.local/bin/uv" /usr/local/bin/uv 2>/dev/null || true
  fi
  docker compose version >/dev/null || die "docker compose plugin is required"
}

checkout_repo() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    log "updating repo at $INSTALL_DIR…"
    git -C "$INSTALL_DIR" fetch origin
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  else
    log "cloning into $INSTALL_DIR…"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    # Allow file:// URLs and local paths for testing/air-gapped installs.
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

# set_key KEY VALUE — replace or append KEY=VALUE in .env (value never echoed).
set_key() {
  local key="$1" value="$2" env_file="$INSTALL_DIR/.env"
  [ -n "$value" ] || return 0
  local esc
  esc=$(printf '%s' "$value" | sed 's/[\/&]/\\&/g')
  if grep -qE "^${key}=" "$env_file"; then
    sed -i "s/^${key}=.*/${key}=${esc}/" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$env_file"
  fi
}

ask() { # ask VAR PROMPT DEFAULT — fills VAR from env or prompt (silent for secrets when SECRET=1)
  local var="$1" prompt="$2" default="${3:-}" val secret="${SECRET:-0}"
  eval "val=\${$var:-}"
  [ -n "$val" ] && return 0
  [ "$YES" = 1 ] && { eval "$var=\"\$default\""; return 0; }
  if [ "$secret" = 1 ]; then
    printf '%s: ' "$prompt" >&2; IFS= read -rs val || true; echo >&2
  else
    printf '%s [%s]: ' "$prompt" "$default" >&2; IFS= read -r val || true
    [ -n "$val" ] || val="$default"
  fi
  eval "$var=\"\$val\""
}

configure_env() {
  local env_file="$INSTALL_DIR/.env"
  if [ ! -f "$env_file" ]; then
    log "creating .env from .env.example…"
    cp "$INSTALL_DIR/.env.example" "$env_file"
    chmod 600 "$env_file"
  else
    log ".env exists — keeping it (missing keys will be added)…"
  fi
  # Ensure keys introduced after the .env was created exist.
  for key in LEASEWEB_API_KEY LEASEWEB_API_BASE_URL ZARINPAL_MERCHANT_ID ZARINPAL_SANDBOX ZARINPAL_CALLBACK_URL ARVANCLOUD_API_KEY; do
    grep -qE "^${key}=" "$env_file" || printf '%s=\n' "$key" >> "$env_file"
  done

  log "collecting secrets (Enter = keep current/empty; nothing is printed back)…"
  ask TELEGRAM_BOT_TOKEN "Telegram bot token (from @BotFather)" ""
  ask CALLBACK_SIGNING_KEY "Callback signing key (empty = auto-generate)" ""
  [ -z "${CALLBACK_SIGNING_KEY:-}" ] && CALLBACK_SIGNING_KEY=$(openssl rand -hex 32)
  ask PROVIDER_CREDENTIAL_ENCRYPTION_KEY "Provider credential encryption key (empty = auto-generate)" ""
  [ -z "${PROVIDER_CREDENTIAL_ENCRYPTION_KEY:-}" ] && PROVIDER_CREDENTIAL_ENCRYPTION_KEY=$(openssl rand -hex 32)

  case ",$PROVIDERS," in
    *,hetzner,*) ask HETZNER_API_TOKEN "Hetzner API token" "" ; SECRET=1 ask HETZNER_API_TOKEN "Hetzner API token (repeat to confirm stored)" "${HETZNER_API_TOKEN:-}" ;;
  esac
  # NOTE: ask() already filled the var from env when non-interactive; the SECRET
  # indirection above only affects prompting. Read values back explicitly:
  :
  if [ "${YES:-0}" != 1 ]; then
    if [[ ",$PROVIDERS," == *,hetzner,* ]]; then SECRET=0; fi
    if [[ ",$PROVIDERS," == *,leaseweb,* ]]; then
      SECRET=1 ask LEASEWEB_API_KEY "LeaseWeb API key (X-LSW-Auth)" "" ; SECRET=0
    fi
    SECRET=1 ask ZARINPAL_MERCHANT_ID "ZarinPal merchant id (empty = skip payments)" "" ; SECRET=0
  fi

  set_key TELEGRAM_BOT_TOKEN "${TELEGRAM_BOT_TOKEN:-}"
  set_key CALLBACK_SIGNING_KEY "${CALLBACK_SIGNING_KEY:-}"
  set_key PROVIDER_CREDENTIAL_ENCRYPTION_KEY "${PROVIDER_CREDENTIAL_ENCRYPTION_KEY:-}"
  set_key HETZNER_API_TOKEN "${HETZNER_API_TOKEN:-}"
  set_key LEASEWEB_API_KEY "${LEASEWEB_API_KEY:-}"
  set_key ZARINPAL_MERCHANT_ID "${ZARINPAL_MERCHANT_ID:-}"
  [ -n "${ZARINPAL_SANDBOX:-}" ] && set_key ZARINPAL_SANDBOX "$ZARINPAL_SANDBOX"
  [ -n "${ZARINPAL_CALLBACK_URL:-}" ] && set_key ZARINPAL_CALLBACK_URL "$ZARINPAL_CALLBACK_URL"
  chmod 600 "$env_file"
  log ".env ready (permissions 600)."
}

compose() { docker compose --project-directory "$INSTALL_DIR" "$@"; }

deploy() {
  log "building images…"
  compose build
  log "starting postgres + redis…"
  compose up -d postgres redis
  log "running migrations…"
  compose run --rm api migrate
  if [ "$DO_SYNC" = 1 ]; then
    IFS=',' read -ra _providers <<< "$PROVIDERS"
    for p in "${_providers[@]}"; do
      p=$(echo "$p" | tr -d ' ')
      [ -z "$p" ] && continue
      log "syncing catalog ($p)…"
      compose run --rm api python scripts/sync_catalog.py --provider "$p" || warn "catalog sync ($p) failed — continuing (check the key, re-run: ./platform.sh sync)"
    done
  fi
  if [ "$DO_UP" = 1 ]; then
    if [ "$WITH_BOT" = 1 ]; then
      log "starting api + worker + bot…"
      compose up -d api worker bot
    else
      log "starting api + worker…"
      compose up -d api worker
    fi
  fi
}

smoke() {
  log "health check…"
  sleep 3
  curl -fsS http://127.0.0.1:8000/health/live && echo
  curl -fsS http://127.0.0.1:8000/health/ready && echo || warn "readiness not OK yet — see: docker compose logs api"
}

finish() {
  echo
  log "done. Manage everything from the bash menu:"
  echo "    cd $INSTALL_DIR && ./platform.sh"
  echo
  echo "  Next steps:"
  echo "  1. Open your bot in Telegram and send /menu"
  echo "  2. Publish a price book + enable offers (docs/operations/RUNBOOK.md)"
  echo "  3. Top up a wallet via ZarinPal, then order a Hetzner/LeaseWeb server"
}

main() {
  need_root
  install_prereqs
  checkout_repo
  configure_env
  deploy
  smoke || true
  finish
}

main "$@"
