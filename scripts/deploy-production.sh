#!/usr/bin/env bash
# Application-image deployment (idempotent, safe to re-run).
#
# Shared deploy engine for the manually approved main release and the automatic
# staging release. Both update the SAME production project, private config,
# database, Redis and Telegram bot. DEPLOY_PROFILE only selects the fast lane.
#
# Ownership model:
#
#   RELEASE-OWNED (travel with the tested Git commit, promoted on success):
#   - the immutable GHCR image (ghcr.io/<org>/<repo>:<full-git-sha>)
#   - the compose contract under test (candidate -> canonical promotion)
#   - migration code and this script's release behavior
#
#   SERVER-OWNED (never delivered from Git, never overwritten by a deploy):
#   - ${DEPLOY_PATH}/deploy.env (only its PLATFORM_IMAGE line is rewritten)
#   - ${CONFIGURATION_PATH} (checked for existence, never touched)
#   - persistent Docker volumes/data and host credentials
#
# Server layout:
#
#   ${DEPLOY_PATH}/docker-compose.yml                 canonical release compose
#   ${DEPLOY_PATH}/.docker-compose.<FULL_SHA>.candidate.yml   release candidate
#   ${DEPLOY_PATH}/deploy.env                         infrastructure values (mode 600)
#   ${CONFIGURATION_PATH}                             server-owned application TOML
#
# The GitHub Actions runner transfers the exact compose file from the exact
# tested commit to the per-SHA candidate path, then invokes this script with
# the candidate path plus its expected SHA-256. Forward deployment runs
# ENTIRELY against the candidate; the canonical file is replaced atomically
# (same-filesystem rename) only after every health/stability/revision gate
# passes. A failed release leaves the canonical contract untouched. Only a
# release that attempted no schema transition may restart the previous image;
# otherwise writers remain stopped for operator recovery. The database is NEVER downgraded.
#
# Required environment:
#   DEPLOY_PATH              operator-owned directory (e.g. /opt/cloud-server-seller)
#   PLATFORM_IMAGE_NEW       new image, exactly ghcr.io/<org>/<repo>:<40-hex-sha>
#   CANDIDATE_COMPOSE_FILE   per-release candidate compose path on the server
#   EXPECTED_COMPOSE_SHA256  SHA-256 of the exact release compose file (64 hex)
#
# Optional environment:
#   CURRENT_COMPOSE_FILE   default: ${DEPLOY_PATH}/docker-compose.yml
#   ENV_FILE               default: ${DEPLOY_PATH}/deploy.env
#   CONFIGURATION_PATH     default: /etc/cloud-server-seller/configuration.toml
#   EXPECTED_HEAD          alembic head revision the database must report
#   HEALTH_ATTEMPTS        default: 96 (x HEALTH_INTERVAL seconds for the API)
#   HEALTH_INTERVAL        default: 5 (seconds between health probes)
#   STABILIZE_SECONDS      default: 15 (worker/bot restart-stability window)
#   DEPLOY_PROFILE         default: production. `staging` keeps every gate
#                          below but SKIPS the one-shot provider catalog
#                          refresh, which the worker's scheduled coordinator
#                          already runs every storefront_catalog_sync_interval
#                          and which would add minutes to every development
#                          deploy. No other behavior changes with profile.
#
# Sequence: validate files -> save rollback image -> switch PLATFORM_IMAGE ->
# pull -> postgres/redis healthy -> migrate (alembic upgrade head) ->
# database migration head == image head -> database PHYSICAL schema matches the
# release -> catalog refresh (catalog auto-sync run: provider facts, pricing,
# publication; production profile only - see DEPLOY_PROFILE) -> catalog
# canonicalization (offers normalize-selling-currency) ->
# api, worker, exactly one bot -> health/readiness -> STOREFRONT READINESS
# (offers readiness: an enabled+credentialed+auto-priced provider with stored
# offers but none sellable fails the release) -> report. On failure rollback
# never downgrades the database and never assumes an older application can
# interpret a forward-migrated financial epoch.
# A successful rollback is still a FAILED deployment (exit 1).
#
# Fail-closed design: every validation and every deployment step propagates
# failure with an EXPLICIT `return 1` (or an `if ! ...` block ending in
# `return 1`). Nothing here relies on `set -e`, because `deploy()` callers
# must be free to branch on its status without errexit being suppressed.
# Preflight failures return before anything is mutated, so they never
# trigger a rollback; only failures at/after the image switch roll back.
#
# This script never prints secrets or changes secret values: deploy.env is
# touched only on its PLATFORM_IMAGE line. The mounted configuration is
# validated inside the image without echoing validation inputs. GHCR login
# happens outside this script with a short-lived, cleaned-up Docker config.
set -Eeuo pipefail

IMAGE_RE='^ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+:[0-9a-f]{40}$'
PREV_IMAGE=""
ROLLED_BACK="no"
MUTATED="no"
MIGRATION_STARTED="no"
DB_HEAD_BEFORE=""
WRITERS_STOPPED="no"
APP_START_ATTEMPTED="no"

load_config() {
    # Resolved here (not at file scope) so the functions below can be
    # sourced without side effects (see DEPLOY_PRODUCTION_SOURCED).
    DEPLOY_PATH="${DEPLOY_PATH:?set DEPLOY_PATH (e.g. /opt/cloud-server-seller)}"
    PLATFORM_IMAGE_NEW="${PLATFORM_IMAGE_NEW:?set PLATFORM_IMAGE_NEW (ghcr.io/<org>/<repo>:<full-sha>)}"
    CURRENT_COMPOSE_FILE="${CURRENT_COMPOSE_FILE:-${DEPLOY_PATH}/docker-compose.yml}"
    CANDIDATE_COMPOSE_FILE="${CANDIDATE_COMPOSE_FILE:?set CANDIDATE_COMPOSE_FILE (per-release candidate path)}"
    EXPECTED_COMPOSE_SHA256="${EXPECTED_COMPOSE_SHA256:?set EXPECTED_COMPOSE_SHA256 (64 hex chars)}"
    ENV_FILE="${ENV_FILE:-${DEPLOY_PATH}/deploy.env}"
    CONFIGURATION_PATH="${CONFIGURATION_PATH:-/etc/cloud-server-seller/configuration.toml}"
    EXPECTED_HEAD="${EXPECTED_HEAD:-}"
    # 480s accommodates 120s cold startup plus the service-specific probe cadence
    # and retries under CPU contention. No readiness/heartbeat gate is skipped.
    HEALTH_ATTEMPTS="${HEALTH_ATTEMPTS:-96}"
    HEALTH_INTERVAL="${HEALTH_INTERVAL:-5}"
    STABILIZE_SECONDS="${STABILIZE_SECONDS:-15}"
    DEPLOY_PROFILE="${DEPLOY_PROFILE:-production}"
    PROJECT="cloud-platform-production"
    export PLATFORM_NETWORK="${PROJECT}-bridge"
}

log() {
    printf '[deploy] %s\n' "$*"
}

fail() {
    printf '[deploy] FAIL: %s\n' "$*" >&2
    return 1
}

compose_candidate() {
    # Forward deployment ALWAYS uses the release candidate contract.
    PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" docker compose --project-name "${PROJECT}" --env-file "${ENV_FILE}" -f "${CANDIDATE_COMPOSE_FILE}" "$@"
}

compose_current() {
    # Rollback and canonical status ALWAYS use the previous canonical contract.
    docker compose --project-name "${PROJECT}" --env-file "${ENV_FILE}" -f "${CURRENT_COMPOSE_FILE}" "$@"
}

file_sha256() {
    sha256sum "$1" | awk '{print $1}'
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
    tmp="$(mktemp "${file}.tmp.XXXXXX")" || { fail "cannot create temp file"; return 1; }
    chmod --reference="${file}" "${tmp}" && chown --reference="${file}" "${tmp}" || {
        rm -f "${tmp}"
        fail "cannot preserve deployment env ownership/permissions"
        return 1
    }
    if grep -qE '^PLATFORM_IMAGE=' "${file}"; then
        awk -v img="${image}" '{ if ($0 ~ /^PLATFORM_IMAGE=/) print "PLATFORM_IMAGE=" img; else print }' \
            "${file}" > "${tmp}" || { fail "cannot rewrite ${file}"; rm -f "${tmp}"; return 1; }
    else
        cat "${file}" > "${tmp}" || { fail "cannot read ${file}"; rm -f "${tmp}"; return 1; }
        printf 'PLATFORM_IMAGE=%s\n' "${image}" >> "${tmp}" \
            || { fail "cannot append to temp file"; rm -f "${tmp}"; return 1; }
    fi
    mv -f "${tmp}" "${file}" || { fail "cannot atomically update ${file}"; rm -f "${tmp}"; return 1; }
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
    docker inspect -f '{{.RestartCount}}' "${id}" 2>/dev/null
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
    local port="$1"
    python3 -c 'import json,sys,urllib.request
try:
    with urllib.request.urlopen(sys.argv[1],timeout=5) as response:
        payload=json.load(response)
        ok=response.status == 200 and isinstance(payload,dict) and payload.get("status") == "ok"
except Exception:
    ok=False
sys.exit(0 if ok else 1)' "http://127.0.0.1:${port}/health/ready" >/dev/null 2>&1
}

rollback() {
    # A first deployment can partially start too: stop candidate writers
    # BEFORE checking whether a previous image exists.
    if [ "${WRITERS_STOPPED}" = "yes" ] || [ "${APP_START_ATTEMPTED}" = "yes" ]; then
        compose_candidate stop --timeout 45 api worker bot >/dev/null || {
            fail "ROLLBACK: cannot stop candidate writers; refusing another poller"
            return 1
        }
    fi
    if [ -z "${PREV_IMAGE}" ]; then
        log "ROLLBACK: no previous image recorded; candidate writers remain stopped"
        return 0
    fi
    if [ "${APP_START_ATTEMPTED}" = "yes" ]; then
        local previous_contract new_contract
        previous_contract="$(docker image inspect -f '{{index .Config.Labels "io.servercloud.platform-contract"}}' "${PREV_IMAGE}")" || return 1
        new_contract="$(docker image inspect -f '{{index .Config.Labels "io.servercloud.platform-contract"}}' "${PLATFORM_IMAGE_NEW}")" || return 1
        if [ -z "${previous_contract}" ] || [ "${previous_contract}" != "${new_contract}" ]; then
            fail "ROLLBACK: previous image does not support the activated integration contract; writers remain stopped"
            return 1
        fi
    fi
    if [ "${MIGRATION_STARTED}" = "yes" ]; then
        local observed
        observed="$(db_head)" || { fail "ROLLBACK: cannot establish database revision; writers remain stopped"; return 1; }
        # A failed migration may have changed data before stamping its revision.
        # Physical parity cannot prove that old code understands a new wallet
        # denomination/financial epoch, even when all its columns still exist.
        if [ "${IMAGE_HEAD:-}" != "${DB_HEAD_BEFORE}" ] || [ "${observed}" != "${DB_HEAD_BEFORE}" ]; then
            fail "ROLLBACK: schema transition attempted (${DB_HEAD_BEFORE:-base} -> ${IMAGE_HEAD:-unknown}); observed ${observed:-base}. Writers remain stopped. Deploy a forward fix or perform separately verified operator recovery; no automatic old-image restart"
            return 1
        fi
        PLATFORM_IMAGE="${PREV_IMAGE}" compose_current run --rm --no-deps migrate \
            python -m cloud_platform.db.schema_parity >/dev/null 2>&1 || {
                fail "ROLLBACK: previous image rejects current physical schema; writers remain stopped"
                return 1
            }
    fi
    log "ROLLBACK: restoring ${PREV_IMAGE}"
    set_platform_image "${ENV_FILE}" "${PREV_IMAGE}" || return 1
    if [ "${WRITERS_STOPPED}" = "yes" ] || [ "${APP_START_ATTEMPTED}" = "yes" ]; then
        PLATFORM_IMAGE="${PREV_IMAGE}" compose_current up -d --no-deps api worker bot >/dev/null || {
            compose_current stop --timeout 45 api worker bot >/dev/null || true
            fail "ROLLBACK: cannot restart the previous application image; writers stopped"
            return 1
        }
        local ids count
        ids="$(compose_current ps -q bot)" || {
            compose_current stop --timeout 45 api worker bot >/dev/null || true
            fail "ROLLBACK: cannot establish bot cardinality; writers stopped"
            return 1
        }
        count="$(printf '%s\n' "${ids}" | grep -c . || true)"
        if [ "${count}" != "1" ] || ! container_running "${ids}"; then
            compose_current stop --timeout 45 api worker bot >/dev/null || true
            fail "ROLLBACK: single running bot not established; writers stopped"
            return 1
        fi
    fi
    ROLLED_BACK="yes"
    log "ROLLBACK: previous application image restored (database NOT downgraded)"
}

# The alembic head the RELEASE IMAGE ships with, read from the image itself.
# A CI variable can drift from the built artifact; the artifact cannot drift
# from itself, so this — not EXPECTED_HEAD — is the authority.
#
# PLATFORM_IMAGE is pinned for BOTH probes: deploy.env still records the
# PREVIOUS image at preflight time, and reading the head from that image would
# compare the database against the schema of the release we are replacing.
# (`run SERVICE COMMAND` REPLACES the service command, so `migrate alembic
# heads` execs `alembic heads` directly and touches no database.)
image_head() {
    local output
    output="$(PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" compose_candidate run --rm --no-deps migrate \
        alembic heads 2>/dev/null)" || return 1
    printf '%s\n' "${output}" \
        | awk 'NF && $0 !~ /^(INFO|WARN|DEBUG|ERROR)/ && $1 ~ /^[0-9a-zA-Z_]+$/ { print $1 }' \
        | sort -u | tr '\n' ' ' | sed 's/ *$//'
}

# The revision the DATABASE currently reports, read with the release image
# whose migrations the database is required to have reached.
db_head() {
    # Keep every revision. Selecting the first row hides a branched database.
    local output
    output="$(PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" compose_candidate run --rm --no-deps migrate \
        alembic current 2>/dev/null)" || return 1
    printf '%s\n' "${output}" \
        | awk 'NF && $0 !~ /^(INFO|WARN|DEBUG|ERROR)/ && $1 ~ /^[0-9a-zA-Z_]+$/ { print $1 }' \
        | sort -u | tr '\n' ' ' | sed 's/ *$//'
}

verify_application_configuration() {
    # Configuration preflight: the NEW release image must accept the
    # server-owned configuration BEFORE anything is mutated (no image switch,
    # no migration, no service replacement, no bot/API restart).
    #
    # It runs the same Settings loader the services use, inside the release
    # image, with the real configuration mounted exactly as the services mount
    # it — so an enabled payment gateway with a callback URL the provider could
    # never reach fails the deployment instead of surfacing the first time a
    # customer presses "pay". The file is mounted by the compose service and
    # is never read, printed or rewritten by this script; validation errors are
    # secret-free (they name fields, never values).
    if ! docker pull "${PLATFORM_IMAGE_NEW}" >/dev/null 2>&1; then
        fail "cannot pull ${PLATFORM_IMAGE_NEW} for the configuration preflight"
        return 1
    fi
    local revision
    revision="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "${PLATFORM_IMAGE_NEW}" 2>/dev/null)" || { fail "cannot inspect release image revision"; return 1; }
    [ "${revision}" = "${PLATFORM_IMAGE_NEW##*:}" ] || {
        fail "release image revision does not match its immutable commit tag"
        return 1
    }
    local contract
    contract="$(docker image inspect -f '{{index .Config.Labels "io.servercloud.platform-contract"}}' \
        "${PLATFORM_IMAGE_NEW}" 2>/dev/null)" || { fail "cannot inspect platform contract"; return 1; }
    [ "${contract}" = "1" ] || {
        fail "release image does not support the canonical platform contract"; return 1;
    }
    if ! PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" compose_candidate run --rm --no-deps migrate \
        python -c "from cloud_platform.core.config import get_settings; s=get_settings(); valid=(s.app_env == 'production' and s.default_currency == 'IRT' and s.fx_default_display_currency == 'IRT' and not s.api_allow_header_identity and len(s.callback_signing_key.encode()) >= 32 and s.fx_enabled and s.fx_domestic_enabled and s.fx_allow_usdt_proxy_for_settlement); raise SystemExit(0 if valid else 1)" >/dev/null 2>&1; then
        fail "server configuration rejected by the release image (validate the server-owned configuration locally; nothing was changed)"
        return 1
    fi
    log "server configuration accepted by the release image"

    # Migration-head preflight (still before ANY mutation). Application code and
    # the database schema must be released together: code that queries a table
    # the database does not have must never reach a running service. The head is
    # read from the IMAGE, and EXPECTED_HEAD (when the pipeline provides it) is
    # cross-checked against it so CI/artifact drift is caught here too.
    IMAGE_HEAD="$(image_head)"
    [ -n "${IMAGE_HEAD}" ] || {
        fail "cannot determine the alembic head shipped in ${PLATFORM_IMAGE_NEW}"
        return 1
    }
    case "${IMAGE_HEAD}" in
        *" "*)
            fail "the release image ships MULTIPLE alembic heads (${IMAGE_HEAD}); merge them before deploying"
            return 1
            ;;
    esac
    if [ -n "${EXPECTED_HEAD}" ] && [ "${EXPECTED_HEAD}" != "${IMAGE_HEAD}" ]; then
        fail "EXPECTED_HEAD ${EXPECTED_HEAD} does not match the head shipped in the image ${IMAGE_HEAD} (pipeline/artifact drift); nothing was changed"
        return 1
    fi
    log "release image alembic head: ${IMAGE_HEAD}"
}

deploy() {
    load_config
    command -v docker >/dev/null || { fail "docker is not installed"; return 1; }
    docker compose version >/dev/null || { fail "docker compose plugin is not available"; return 1; }
    command -v python3 >/dev/null || { fail "python3 is not installed (needed for health probes)"; return 1; }
    command -v sha256sum >/dev/null || { fail "sha256sum is not installed (needed for compose integrity)"; return 1; }

    [ -f "${ENV_FILE}" ] || { fail "deployment env file missing: ${ENV_FILE}"; return 1; }
    # Server-owned configuration: required, but never created or modified here.
    [ -f "${CONFIGURATION_PATH}" ] || {
        fail "server configuration missing: ${CONFIGURATION_PATH} (bootstrap it once, deploys never touch it)"
        return 1
    }

    # Release-candidate integrity: verified BEFORE any deployment mutation.
    # A failure here changes nothing (no image switch, no migration, no
    # restarts, no rollback) because nothing has been mutated yet.
    [ "${CANDIDATE_COMPOSE_FILE}" != "${CURRENT_COMPOSE_FILE}" ] || {
        fail "candidate and canonical compose must be different files"
        return 1
    }
    [ -f "${CANDIDATE_COMPOSE_FILE}" ] || {
        fail "release candidate compose missing: ${CANDIDATE_COMPOSE_FILE}"
        return 1
    }
    [ ! -L "${CANDIDATE_COMPOSE_FILE}" ] || {
        fail "release candidate compose must not be a symlink: ${CANDIDATE_COMPOSE_FILE}"
        return 1
    }
    if ! printf '%s' "${EXPECTED_COMPOSE_SHA256}" | grep -Eq '^[0-9a-f]{64}$'; then
        fail "EXPECTED_COMPOSE_SHA256 must be 64 hex chars"
        return 1
    fi
    candidate_sha="$(file_sha256 "${CANDIDATE_COMPOSE_FILE}")" \
        || { fail "cannot hash release candidate compose"; return 1; }
    [ "${candidate_sha}" = "${EXPECTED_COMPOSE_SHA256}" ] || {
        fail "release candidate compose SHA-256 mismatch (expected ${EXPECTED_COMPOSE_SHA256}, got ${candidate_sha})"
        return 1
    }
    log "release candidate compose verified: ${candidate_sha}"
    # The candidate must parse with the NEW release image, not whatever old
    # PLATFORM_IMAGE happens to sit in deploy.env.
    PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" compose_candidate config >/dev/null \
        || { fail "release candidate compose does not parse"; return 1; }

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
    if ! printf '%s' "${HEALTH_ATTEMPTS}" | grep -Eq '^[1-9][0-9]{0,8}$'; then
        fail "HEALTH_ATTEMPTS must be a positive decimal integer without leading zeros"
        return 1
    fi
    if ! printf '%s' "${HEALTH_INTERVAL}" | grep -Eq '^[1-9][0-9]{0,8}$'; then
        fail "HEALTH_INTERVAL must be a positive decimal integer without leading zeros"
        return 1
    fi
    case "${STABILIZE_SECONDS}" in
        '' | *[!0-9]*) fail "STABILIZE_SECONDS must be a non-negative integer"; return 1 ;;
    esac
    case "${DEPLOY_PROFILE}" in
        production | staging) ;;
        *)
            fail "DEPLOY_PROFILE must be 'production' or 'staging', got '${DEPLOY_PROFILE}'"
            return 1
            ;;
    esac
    existing_projects="$(docker ps -a \
        --filter "label=com.docker.compose.project.working_dir=${DEPLOY_PATH}" \
        --format '{{.Label "com.docker.compose.project"}}')" || {
        fail "cannot inspect existing deployment ownership"; return 1;
    }
    while IFS= read -r existing_project; do
        if [ -n "${existing_project}" ] && [ "${existing_project}" != "${PROJECT}" ]; then
            fail "deployment path belongs to another environment/project; nothing was changed"
            return 1
        fi
    done <<< "${existing_projects}"
    log "deploy profile      : ${DEPLOY_PROFILE}"

    # Still before the mutation window: a rejected configuration must leave
    # the running release completely untouched.
    verify_application_configuration || { fail "deployment aborted before anything was mutated"; return 1; }

    PREV_IMAGE="$(dotenv_value "${ENV_FILE}" PLATFORM_IMAGE)"
    if [ -n "${PREV_IMAGE}" ]; then
        printf '%s' "${PREV_IMAGE}" | grep -Eq "${IMAGE_RE}" || {
            fail "recorded previous image is not an immutable commit image"
            return 1
        }
        [ -f "${CURRENT_COMPOSE_FILE}" ] || {
            fail "canonical compose missing for the recorded previous release"
            return 1
        }
    fi
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
    if ! compose_candidate pull migrate api worker bot; then
        log "pull failed — restoring previous image reference"
        if [ -n "${PREV_IMAGE}" ]; then
            set_platform_image "${ENV_FILE}" "${PREV_IMAGE}" || return 1
        fi
        return 1
    fi

    log "starting postgres + redis"
    compose_candidate up -d postgres redis >/dev/null || { fail "cannot start postgres/redis"; return 1; }

    log "waiting for postgres/redis health"
    for service in postgres redis; do
        id="$(compose_candidate ps -q "${service}" | head -n 1)"
        [ -n "${id}" ] || { fail "no container id for ${service}"; return 1; }
        wait_healthy "${id}" 24 "${HEALTH_INTERVAL}" \
            || { fail "${service} did not become healthy"; return 1; }
    done

    DB_HEAD_BEFORE="$(db_head)" || { fail "cannot establish pre-migration revision"; return 1; }
    case "${DB_HEAD_BEFORE}" in
        *" "*) fail "database reports multiple migration revisions; refusing schema changes"; return 1 ;;
    esac
    log "stopping application writers before schema changes (including the single bot)"
    MUTATED="yes"
    WRITERS_STOPPED="yes"
    if [ -f "${CURRENT_COMPOSE_FILE}" ]; then
        compose_current stop --timeout 45 api worker bot >/dev/null || {
            fail "cannot stop existing application writers; refusing migrations"
            return 1
        }
    fi
    log "creating encrypted pre-migration database backup"
    if ! compose_candidate --profile backup run --rm backup; then
        fail "encrypted pre-migration backup failed; refusing schema changes"
        return 1
    fi
    log "running database migrations (alembic upgrade head)"
    MIGRATION_STARTED="yes"
    if ! compose_candidate run --rm --no-deps migrate; then
        fail "migration failed; database left as-is and rollback compatibility must be established"
        return 1
    fi
    log "migrations applied"

    # GATE: the database must now BE at the image's head, BEFORE any
    # schema-dependent service starts. This is the check whose absence let a
    # production deployment run code (provider_routes) against a database that
    # did not contain the table.
    DB_HEAD="$(db_head)"
    if [ "${DB_HEAD}" != "${IMAGE_HEAD}" ]; then
        log "alembic current: ${DB_HEAD:-<empty>} (image head: ${IMAGE_HEAD})"
        fail "database is NOT at the image alembic head ${IMAGE_HEAD}; refusing to start api/worker/bot on a mismatched schema"
        return 1
    fi
    log "database migration head verified: ${DB_HEAD} (== image head)"

    # GATE: the revision is NOT evidence of a compatible schema.
    #
    # Production proved it: the database reported revision 0037 while objects
    # created by migration 0035 (provider_routes) and the credential-account
    # columns were physically ABSENT. The head comparison above PASSES on such
    # a database, and the release then fails at runtime with
    # `relation "provider_routes" does not exist`. So the release verifies the
    # physical schema its own code queries - tables, columns and the uniqueness
    # its upserts rely on - read-only, with the release image, BEFORE any
    # schema-dependent service is replaced. A drifted database is repaired
    # forward by the migration step above (see migration 0038); if it is still
    # missing objects, nothing was started and this deploy stops here.
    if ! PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" compose_candidate run --rm --no-deps migrate \
        python -m cloud_platform.db.schema_parity; then
        fail "database physical schema does not support the release (missing objects listed above); refusing to start api/worker/bot"
        return 1
    fi
    log "database physical schema verified against the release"

    # GATE: refresh provider facts first (bounded, idempotent release transition).
    #
    # Canonicalization below cannot invent a provider fact that was never
    # stored: a row whose exact provider rate is missing, or whose integer cost
    # was stored under a wrong minor-unit assumption, is left fail-closed until
    # the provider is re-observed. So the release runs ONE complete catalog
    # refresh through the same advisory-locked coordinator the worker cron uses
    # (official read-only APIs, server-owned markup, publication), bounded by
    # the DEDICATED catalog timeout — not the generic 120s job timeout whose
    # cancellation is what left a production catalog unpriced.
    #
    # A non-zero exit is NOT fatal — a provider/API outage must not block an
    # unrelated release — but it is never silent, and the storefront readiness
    # gate after the services start stays authoritative.
    if [ "${DEPLOY_PROFILE}" = "staging" ]; then
        # Staging profile: the worker already owns the periodic refresh (the
        # advisory-locked coordinator, every storefront_catalog_sync_interval),
        # so running the full bounded pass here would add minutes of provider
        # I/O to every development deploy for no new fact. Every gate that
        # follows (services, health, storefront readiness) is unchanged.
        log "skipping the one-shot catalog refresh (staging profile: the worker's scheduled sync owns provider facts)"
    else
        log "refreshing provider catalog facts (bounded one-shot sync)"
        if PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" compose_candidate run --rm --no-deps migrate \
            python -m cloud_platform.cli catalog auto-sync run; then
            log "catalog refresh completed"
        else
            log "[WARN] catalog refresh reported provider errors (counts above); the storefront readiness gate below is authoritative"
        fi
    fi

    # GATE: catalog canonicalization (idempotent release transition).
    #
    # Migration 0043 deliberately refuses to invent financial history, and the
    # release that switched the storefront to canonical-currency/provenance
    # visibility could not enforce it either: every pre-existing foreign row
    # became invisible while every health check stayed green — a GREEN deploy
    # with an EMPTY customer catalog. So the release canonicalizes the catalog
    # itself through the supported, audited CLI path an operator would use
    # (exact FX, single markup on auto-priced rows, NO second markup on manual
    # prices, operator-disabled rows untouched, fail-closed on missing facts),
    # in a one-shot container BEFORE any customer-facing service is replaced.
    # It is idempotent: an already canonical catalog is a no-op.
    #
    # A non-zero exit is NOT fatal here — an FX outage must not block an
    # unrelated release — but it is never silent: the counters are printed and
    # the storefront readiness gate after the services start is authoritative.
    log "canonicalizing catalog selling currency (idempotent release transition)"
    if PLATFORM_IMAGE="${PLATFORM_IMAGE_NEW}" compose_candidate run --rm --no-deps migrate \
        python -m cloud_platform.cli offers normalize-selling-currency --execute; then
        log "catalog selling currency is canonical"
    else
        log "[WARN] catalog normalization reported failures (counts above); the storefront readiness gate below is authoritative"
    fi

    log "starting api + worker + bot (bot replicas = 1)"
    APP_START_ATTEMPTED="yes"
    # Migration already completed and schema/dependency health were verified.
    # Resolving Compose dependencies here would run the migrate service again.
    compose_candidate up -d --no-deps api worker bot >/dev/null || { fail "cannot start api/worker/bot"; return 1; }

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
        id="$(compose_candidate ps -q "${service}" | head -n 1)"
        container_healthy "${id}" || { fail "${service} is not healthy"; return 1; }
    done
    for service in worker; do
        id="$(compose_candidate ps -q "${service}" | head -n 1)"
        [ -n "${id}" ] || { fail "${service} has no container"; return 1; }
        container_running "${id}" || { fail "${service} is not running"; return 1; }
        worker_id="${id}"
    done

    # Telegram long polling is a single-consumer transport: exactly one bot.
    local bot_count bot_id
    bot_id="$(compose_candidate ps -q bot)" || return 1
    bot_count="$(printf '%s\n' "${bot_id}" | grep -c . || true)"
    [ "${bot_count}" = "1" ] || {
        fail "bot replica invariant violated: ${bot_count} bot containers (want exactly 1)"
        return 1
    }
    container_running "${bot_id}" || { fail "bot container is not running"; return 1; }
    log "bot replicas: exactly 1, running"
    for service in api worker bot; do
        id="$(compose_candidate ps -q "${service}")" || return 1
        [ -n "${id}" ] && wait_healthy "${id}" "${HEALTH_ATTEMPTS}" "${HEALTH_INTERVAL}" || {
            fail "${service} did not establish its service-specific health signal"
            return 1
        }
    done

    # Startup-stability gate: a crash-looping container can look "running"
    # for a moment between restarts. Require the SAME worker/bot containers
    # to still be running with unchanged restart counts after a short
    # bounded wait. No HTTP probing: neither service serves HTTP.
    local bot_restarts_before worker_restarts_before
    bot_restarts_before="$(restart_count "${bot_id}")" || { fail "cannot inspect bot restart count"; return 1; }
    worker_restarts_before="$(restart_count "${worker_id}")" || { fail "cannot inspect worker restart count"; return 1; }
    printf '%s:%s' "${bot_restarts_before}" "${worker_restarts_before}" | grep -Eq '^[0-9]+:[0-9]+$' || {
        fail "invalid worker/bot restart counters"
        return 1
    }
    log "waiting ${STABILIZE_SECONDS}s for worker/bot startup stabilization"
    sleep "${STABILIZE_SECONDS}"
    local bot_id_after worker_id_after
    bot_id_after="$(compose_candidate ps -q bot)" || return 1
    worker_id_after="$(compose_candidate ps -q worker | head -n 1)"
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
        fail "bot restarted during stabilization (crash loop?)"
        return 1
    fi
    if [ "$(restart_count "${worker_id}")" != "${worker_restarts_before}" ]; then
        fail "worker restarted during stabilization"
        return 1
    fi
    log "worker + bot stable (same containers running, no restarts)"

    log "verifying migration revision"
    local current
    current="$(db_head)" || { fail "cannot recheck database migration revision"; return 1; }
    [ "${current}" = "${IMAGE_HEAD}" ] || {
        fail "database is not at expected head ${IMAGE_HEAD}"
        return 1
    }
    log "alembic current reports expected head ${IMAGE_HEAD}"

    # GATE: the customer-facing catalog must actually be open.
    #
    # Healthy services are NOT evidence that a customer can buy anything: the
    # global-USD release deployed green with 507 stored offers and ZERO on
    # sale. This runs the machine-usable, provider-neutral readiness assertion
    # INSIDE the running release (read-only, no provider call) before the
    # compose contract is promoted, so a release that would leave an enabled, credentialed
    # and auto-priced provider with nothing on sale fails here — with the
    # rollback the caller performs on any failure — instead of silently
    # showing every customer "nothing is for sale".
    log "verifying storefront readiness (sellable offers per enabled provider)"
    if ! compose_candidate exec -T api python -m cloud_platform.cli offers readiness; then
        fail "storefront readiness failed: an enabled, credentialed, auto-priced provider has NOTHING on sale; refusing to promote this release (see the action above, then redeploy)"
        return 1
    fi
    log "storefront readiness: ok"

    log "verifying read-only database and worker smoke (not just API liveness)"
    compose_candidate exec -T api python scripts/post_deploy_smoke.py \
        --base-url http://127.0.0.1:8000 --expected-head "${IMAGE_HEAD}" || {
            fail "database/worker post-deploy smoke failed"
            return 1
        }

    # Make promotion the LAST fallible step: a failure must still leave the
    # previous canonical compose available for compatible image rollback.
    log "service status:"
    compose_candidate ps || { fail "cannot list service status"; return 1; }
    chmod 0644 "${CANDIDATE_COMPOSE_FILE}" \
        || { fail "cannot set candidate compose permissions"; return 1; }
    promoted_sha="$(file_sha256 "${CANDIDATE_COMPOSE_FILE}")" \
        || { fail "cannot hash candidate compose before promotion"; return 1; }
    [ "${promoted_sha}" = "${EXPECTED_COMPOSE_SHA256}" ] || {
        fail "candidate compose changed during deployment; refusing promotion"
        return 1
    }
    log "promoting release compose to canonical"
    mv "${CANDIDATE_COMPOSE_FILE}" "${CURRENT_COMPOSE_FILE}" \
        || { fail "cannot promote release compose to canonical"; return 1; }
    log "release compose promoted: ${promoted_sha}"
    return 0
}

main() {
    load_config
    cd "${DEPLOY_PATH}" || { fail "cannot enter deploy path: ${DEPLOY_PATH}"; return 1; }
    # The lock is shared by both lanes AND direct operator invocations.
    # Keep it until this process exits; never unlink a flock inode.
    command -v flock >/dev/null || { fail "flock is required for shared-host deployment safety"; return 1; }
    exec 9> "${DEPLOY_PATH}/.deployment.lock" || return 1
    flock -n 9 || { fail "another deployment owns the shared-host lock"; return 1; }
    exec 8> /var/lock/servercloud-platform-release.lock || {
        fail "cannot open the shared website/platform release lock"; return 1;
    }
    flock -w 600 8 || {
        fail "another website/platform release is still active; retry the current SHA"; return 1;
    }
    # SSH disconnect/cancellation must not interrupt schema changes or leave
    # half-replaced services. Both workflows also disable auto-cancellation.
    trap '' HUP INT TERM
    # `deploy` is intentionally NOT run as an `if` condition: errexit is
    # suppressed inside condition contexts, so its status is captured
    # explicitly instead of relying on `set -e`.
    local rc=0
    deploy || rc=$?
    if [ "${rc}" = "0" ]; then
        log "DEPLOYMENT SUCCEEDED: ${PLATFORM_IMAGE_NEW}"
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
    # A failed release must not leave its candidate behind as a stale
    # artifact (the next release ships its own per-SHA candidate). Cleanup
    # failure must never mask the real deployment failure.
    if [ "${CANDIDATE_COMPOSE_FILE}" != "${CURRENT_COMPOSE_FILE}" ]; then
        rm -f "${CANDIDATE_COMPOSE_FILE}" || true
    fi
    return 1
}

# Sourcing the file with DEPLOY_PRODUCTION_SOURCED=1 loads the functions
# without running a deployment (used by the unit tests).
if [ "${DEPLOY_PRODUCTION_SOURCED:-}" != "1" ]; then
    main "$@"
fi
