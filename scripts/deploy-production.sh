#!/usr/bin/env bash
# Production application-image deployment (idempotent, safe to re-run).
#
# Deploys ONE immutable GHCR image (ghcr.io/<org>/<repo>:<full-git-sha>) to the
# production server layout:
#
#   ${DEPLOY_PATH}/docker-compose.yml   compose file (server copy)
#   ${DEPLOY_PATH}/deploy.env           infrastructure values (mode 600)
#   ${CONFIGURATION_PATH}               server-owned application TOML (untouched)
#
# Required environment:
#   DEPLOY_PATH          operator-owned directory (e.g. /opt/cloud-server-seller)
#   PLATFORM_IMAGE_NEW   new image, exactly ghcr.io/<org>/<repo>:<40-hex-sha>
#
# Optional environment:
#   COMPOSE_FILE         default: ${DEPLOY_PATH}/docker-compose.yml
#   ENV_FILE             default: ${DEPLOY_PATH}/deploy.env
#   CONFIGURATION_PATH   default: /etc/cloud-server-seller/configuration.toml
#   EXPECTED_HEAD        alembic head revision the database must report
#   HEALTH_ATTEMPTS      default: 36 (x HEALTH_INTERVAL seconds for the API)
#   HEALTH_INTERVAL      default: 5 (seconds between health probes)
#
# Sequence: validate files -> save rollback image -> switch PLATFORM_IMAGE ->
# pull -> postgres/redis healthy -> migrate (alembic upgrade head) -> api,
# worker, exactly one bot -> health/readiness -> report. On failure the
# previous image is restored and restarted (the database is NEVER downgraded:
# migrations stay forward-compatible so image rollback is always possible).
# A successful rollback is still a FAILED deployment (exit 1).
#
# Fail-closed design: every validation and every deployment step propagates
# failure with an EXPLICIT `return 1` (or an `if ! ...` block ending in
# `return 1`). Nothing here relies on `set -e`, because `deploy()` callers
# must be free to branch on its status without errexit being suppressed.
# Preflight failures return before anything is mutated, so they never
# trigger a rollback; only failures at/after the image switch roll back.
#
# This script never reads, prints, or rewrites secrets: deploy.env is only
# touched on its PLATFORM_IMAGE line, configuration.toml is only checked for
# existence, and GHCR authentication happens outside (GitHub Actions logs in
# with the short-lived GITHUB_TOKEN before invoking this script).
set -Eeuo pipefail

IMAGE_RE='^ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+:[0-9a-f]{40}$'
PREV_IMAGE=""
ROLLED_BACK="no"
MUTATED="no"

load_config() {
    # Resolved here (not at file scope) so the functions below can be
    # sourced without side effects (see DEPLOY_PRODUCTION_SOURCED).
    DEPLOY_PATH="${DEPLOY_PATH:?set DEPLOY_PATH (e.g. /opt/cloud-server-seller)}"
    PLATFORM_IMAGE_NEW="${PLATFORM_IMAGE_NEW:?set PLATFORM_IMAGE_NEW (ghcr.io/<org>/<repo>:<full-sha>)}"
    COMPOSE_FILE="${COMPOSE_FILE:-${DEPLOY_PATH}/docker-compose.yml}"
    ENV_FILE="${ENV_FILE:-${DEPLOY_PATH}/deploy.env}"
    CONFIGURATION_PATH="${CONFIGURATION_PATH:-/etc/cloud-server-seller/configuration.toml}"
    EXPECTED_HEAD="${EXPECTED_HEAD:-}"
    HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-36}"
    HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
    STABILIZE_SECONDS="${STABILIZE_SECONDS:-15}"
}

log() {
    printf '[deploy] %s\n' "$*"
}

fail() {
    printf '[deploy] FAIL: %s\n' "$*" >&2
    return 1
}

compose() {
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" "$@"
}

# Read one KEY=value line from a dotenv file without executing it.
dotenv_value() {
    local file="$1" key="$2" line
    line="$(grep -E "^${key}=" "${file}" | tail -n 1 || true)"
    line="${line#*=}"
    # Strip matching surrounding single or double quotes, if present.
    case "${line}" in
        \"*\"|\'*\') line="${line:1:${#line}-2}" ;;
    esac
    printf '%s' "${line}"
}

set_platform_image() {
    local file="$1" image="$2" tmp
    tmp="$(mktemp)" || { fail "cannot create temp file"; return 1; }
    if grep -qE '^PLATFORM_IMAGE=' "${file}"; then
        awk -v img="${image}" '{ if ($0 ~ /^PLATFORM_IMAGE=/) print "PLATFORM_IMAGE=" img; else print }' \
            "${file}" > "${tmp}" || { fail "cannot rewrite ${file}"; rm -f "${tmp}"; return 1; }
    else
        cat "${file}" > "${tmp}" || { fail "cannot read ${file}"; rm -f "${tmp}"; return 1; }
        printf 'PLATFORM_IMAGE=%s\n' "${image}" >> "${tmp}" \
            || { fail "cannot append to temp file"; rm -f "${tmp}"; return 1; }
    fi
    cat "${tmp}" > "${file}" || { fail "cannot update ${file}"; rm -f "${tmp}"; return 1; }
    rm -f "${tmp}"
}

container_healthy() {
    local name="$1" status
    status="$(docker inspect -f '{{.State.Health.Status}}' "${name}" 2>/dev/null || true)"
    [ "${status}" = "healthy" ]
}

container_running() {
    local id="$1" status
    status="$(docker inspect -f '{{.State.Status}}' "${id}" 2>/dev/null || true)"
    [ "${status}" = "running" ]
}

restart_count() {
    local id="$1"
    docker inspect -f '{{.RestartCount}}' "${id}" 2>/dev/null || true
}

wait_healthy() {
    local name="$1" attempts="${2:-24}" interval="${3:-5}" i
    for ((i = 1; i <= attempts; i++)); do
        if container_healthy "${name}"; then
            return 0
        fi
        sleep "${interval}"
    done
    return 1
}

api_ready() {
    local port="$1" body
    body="$(python3 -c 'import sys,urllib.request;print(urllib.request.urlopen(sys.argv[1],timeout=5).read().decode())' \
        "http://127.0.0.1:${port}/health/ready" 2>/dev/null || true)"
    case "${body}" in
        *'"status": "ok"'*|*'"status":"ok"'*) return 0 ;;
        *) return 1 ;;
    esac
}

rollback() {
    if [ -z "${PREV_IMAGE}" ]; then
        log "ROLLBACK: no previous image recorded (first deploy?) — nothing to restore"
        return 0
    fi
    log "ROLLBACK: restoring ${PREV_IMAGE}"
    set_platform_image "${ENV_FILE}" "${PREV_IMAGE}" || return 1
    compose up -d api worker bot >/dev/null \
        || { fail "ROLLBACK: cannot restart the previous application image"; return 1; }
    ROLLED_BACK="yes"
    log "ROLLBACK: previous application image restarted (database left at the new migration revision)"
}

deploy() {
    load_config
    command -v docker >/dev/null || { fail "docker is not installed"; return 1; }
    docker compose version >/dev/null || { fail "docker compose plugin is not available"; return 1; }
    command -v python3 >/dev/null || { fail "python3 is not installed (needed for health probes)"; return 1; }

    [ -f "${COMPOSE_FILE}" ] || { fail "compose file missing: ${COMPOSE_FILE}"; return 1; }
    [ -f "${ENV_FILE}" ] || { fail "deployment env file missing: ${ENV_FILE}"; return 1; }
    # Server-owned configuration: required, but never created or modified here.
    [ -f "${CONFIGURATION_PATH}" ] || {
        fail "server configuration missing: ${CONFIGURATION_PATH} (bootstrap it once, deploys never touch it)"
        return 1
    }

    case "${PLATFORM_IMAGE_NEW}" in
        *[!a-z0-9.:/_-]*|'') fail "refusing malformed image reference"; return 1 ;;
    esac
    if ! printf '%s' "${PLATFORM_IMAGE_NEW}" | grep -Eq "${IMAGE_RE}"; then
        fail "PLATFORM_IMAGE_NEW must be ghcr.io/<org>/<repo>:<40-hex-sha>, got '${PLATFORM_IMAGE_NEW}'"
        return 1
    fi

    if ! printf '%s' "${DEPLOY_PATH}" | grep -Eq '^/[A-Za-z0-9._/-]+$'; then
        fail "refusing suspicious DEPLOY_PATH: '${DEPLOY_PATH}'"
        return 1
    fi
    case "${HEALTH_ATTEMPTS}" in
        '' | *[!0-9]*) fail "HEALTH_ATTEMPTS must be a positive integer"; return 1 ;;
    esac
    case "${HEALTH_INTERVAL}" in
        '' | *[!0-9]*) fail "HEALTH_INTERVAL must be a positive integer"; return 1 ;;
    esac
    case "${STABILIZE_SECONDS}" in
        '' | *[!0-9]*) fail "STABILIZE_SECONDS must be a non-negative integer"; return 1 ;;
    esac

    PREV_IMAGE="$(dotenv_value "${ENV_FILE}" PLATFORM_IMAGE)"
    log "target commit image : ${PLATFORM_IMAGE_NEW}"
    log "deploy path         : ${DEPLOY_PATH}"
    if [ -n "${PREV_IMAGE}" ]; then
        log "rollback image      : ${PREV_IMAGE}"
    else
        log "rollback image      : (none recorded — first deploy)"
    fi
    if [ -n "${EXPECTED_HEAD}" ]; then
        log "expected alembic head: ${EXPECTED_HEAD}"
    fi

    if [ "${PREV_IMAGE}" = "${PLATFORM_IMAGE_NEW}" ]; then
        log "same image already recorded — continuing idempotently"
    else
        MUTATED="yes"
        set_platform_image "${ENV_FILE}" "${PLATFORM_IMAGE_NEW}" || return 1
        log "PLATFORM_IMAGE updated in ${ENV_FILE} (only that line was touched)"
    fi

    log "pulling application image"
    if ! compose pull migrate api worker bot; then
        log "pull failed — restoring previous image reference"
        if [ -n "${PREV_IMAGE}" ]; then
            set_platform_image "${ENV_FILE}" "${PREV_IMAGE}" || return 1
        fi
        return 1
    fi

    log "starting postgres + redis"
    compose up -d postgres redis >/dev/null || { fail "cannot start postgres/redis"; return 1; }

    log "waiting for postgres/redis health"
    for service in postgres redis; do
        id="$(compose ps -q "${service}" | head -n 1)"
        [ -n "${id}" ] || { fail "no container id for ${service}"; return 1; }
        wait_healthy "${id}" 24 "${HEALTH_INTERVAL}" \
            || { fail "${service} did not become healthy"; return 1; }
    done

    log "running database migrations (alembic upgrade head)"
    if ! compose run --rm --no-deps migrate; then
        log "migration failed — restoring previous image reference (database left as-is)"
        if [ -n "${PREV_IMAGE}" ]; then
            set_platform_image "${ENV_FILE}" "${PREV_IMAGE}" || return 1
        fi
        return 1
    fi
    log "migrations applied"

    log "starting api + worker + bot (bot replicas = 1)"
    compose up -d api worker bot >/dev/null || { fail "cannot start api/worker/bot"; return 1; }

    local api_port
    api_port="$(dotenv_value "${ENV_FILE}" API_PORT)"
    api_port="${api_port:-8000}"

    log "waiting for API readiness on 127.0.0.1:${api_port}/health/ready"
    local i
    for ((i = 1; i <= HEALTH_ATTEMPTS; i++)); do
        if api_ready "${api_port}"; then
            break
        fi
        if [ "${i}" = "${HEALTH_ATTEMPTS}" ]; then
            fail "API did not become ready"
            return 1
        fi
        sleep "${HEALTH_INTERVAL}"
    done
    log "API readiness: ok"

    log "verifying services"
    local id worker_id=""
    for service in postgres redis; do
        id="$(compose ps -q "${service}" | head -n 1)"
        container_healthy "${id}" || { fail "${service} is not healthy"; return 1; }
    done
    for service in worker; do
        id="$(compose ps -q "${service}" | head -n 1)"
        [ -n "${id}" ] || { fail "${service} has no container"; return 1; }
        container_running "${id}" || { fail "${service} is not running"; return 1; }
        worker_id="${id}"
    done

    # Telegram long polling is a single-consumer transport: exactly one bot.
    local bot_count bot_id
    bot_count="$(compose ps -q bot | grep -c . || true)"
    [ "${bot_count}" = "1" ] || {
        fail "bot replica invariant violated: ${bot_count} bot containers (want exactly 1)"
        return 1
    }
    bot_id="$(compose ps -q bot | head -n 1)"
    container_running "${bot_id}" || { fail "bot container is not running"; return 1; }
    log "bot replicas: exactly 1, running"

    # Startup-stability gate: a crash-looping container can look "running"
    # for a moment between restarts. Require the SAME worker/bot containers
    # to still be running with unchanged restart counts after a short
    # bounded wait. No HTTP probing: neither service serves HTTP.
    local bot_restarts_before worker_restarts_before
    bot_restarts_before="$(restart_count "${bot_id}")"
    worker_restarts_before="$(restart_count "${worker_id}")"
    log "waiting ${STABILIZE_SECONDS}s for worker/bot startup stabilization"
    sleep "${STABILIZE_SECONDS}"
    local bot_id_after worker_id_after
    bot_id_after="$(compose ps -q bot | head -n 1)"
    worker_id_after="$(compose ps -q worker | head -n 1)"
    [ "${bot_id_after}" = "${bot_id}" ] || { fail "bot container changed during stabilization"; return 1; }
    [ "${worker_id_after}" = "${worker_id}" ] || {
        fail "worker container changed during stabilization"
        return 1
    }
    container_running "${bot_id}" || { fail "bot container is not running after stabilization"; return 1; }
    container_running "${worker_id}" || {
        fail "worker container is not running after stabilization"
        return 1
    }
    if [ "$(restart_count "${bot_id}")" != "${bot_restarts_before}" ]; then
        compose logs --tail=20 bot 2>/dev/null || true
        fail "bot restarted during stabilization (crash loop?)"
        return 1
    fi
    if [ "$(restart_count "${worker_id}")" != "${worker_restarts_before}" ]; then
        compose logs --tail=20 worker 2>/dev/null || true
        fail "worker restarted during stabilization"
        return 1
    fi
    log "worker + bot stable (same containers running, no restarts)"

    log "verifying migration revision"
    local current
    current="$(compose exec -T api alembic current 2>/dev/null || true)"
    if [ -n "${EXPECTED_HEAD}" ]; then
        case "${current}" in
            *"${EXPECTED_HEAD}"*) log "alembic current reports expected head ${EXPECTED_HEAD}" ;;
            *)
                log "alembic current output: ${current:-<empty>}"
                fail "database is not at expected head ${EXPECTED_HEAD}"
                return 1
                ;;
        esac
    else
        [ -n "${current}" ] || { fail "alembic current is empty"; return 1; }
        log "alembic current: ${current}"
    fi

    log "service status:"
    compose ps || { fail "cannot list service status"; return 1; }
    return 0
}

main() {
    load_config
    cd "${DEPLOY_PATH}" || { fail "cannot enter deploy path: ${DEPLOY_PATH}"; return 1; }
    # `deploy` is intentionally NOT run as an `if` condition: errexit is
    # suppressed inside condition contexts, so its status is captured
    # explicitly instead of relying on `set -e`.
    local rc=0
    deploy || rc=$?
    if [ "${rc}" = "0" ]; then
        log "DEPLOYMENT SUCCEEDED: ${PLATFORM_IMAGE_NEW}"
        # Post-success cleanup only: keep the current and rollback images.
        if [ -n "${PREV_IMAGE}" ] && [ "${PREV_IMAGE}" != "${PLATFORM_IMAGE_NEW}" ]; then
            repo="${PLATFORM_IMAGE_NEW%:*}"
            docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' 2>/dev/null \
                | grep "^${repo}:" \
                | grep -v -e ":${PLATFORM_IMAGE_NEW##*:}$" -e ":${PREV_IMAGE##*:}$" \
                | awk '{print $2}' | sort -u | xargs -r docker rmi >/dev/null 2>&1 || true
            log "old images pruned (current + rollback kept)"
        fi
        return 0
    fi
    # Preflight failures return before anything is mutated: rolling back
    # then would be a fake rollback, so it is skipped explicitly.
    if [ "${MUTATED}" != "yes" ]; then
        log "deployment failed before anything was mutated — no rollback needed"
        return 1
    fi
    log "deployment failed -- attempting application-image rollback"
    if rollback; then
        if [ "${ROLLED_BACK}" = "yes" ]; then
            log "RESULT: DEPLOYMENT FAILED, ROLLBACK DONE (previous image restored; database NOT downgraded)"
        else
            log "RESULT: DEPLOYMENT FAILED, NO ROLLBACK TARGET (first deploy or no previous image)"
        fi
    else
        log "RESULT: DEPLOYMENT FAILED, ROLLBACK ATTEMPT FAILED"
    fi
    return 1
}

# Sourcing the file with DEPLOY_PRODUCTION_SOURCED=1 loads the functions
# without running a deployment (used by the unit tests).
if [ "${DEPLOY_PRODUCTION_SOURCED:-}" != "1" ]; then
    main "$@"
fi
