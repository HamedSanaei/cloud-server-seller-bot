#!/bin/sh
# Image entrypoint (M12-002): one image, several entry points.
#
#   docker run <image> api                # uvicorn on :8000 (default)
#   docker run <image> bot                # Telegram bot (long polling)
#   docker run <image> worker              # arq job runner (all queues)
#   docker run <image> worker-provisioning # arq: provisioning queue only (M16-004)
#   docker run <image> worker-billing      # arq: billing queue only (M16-004)
#   docker run <image> worker-notify       # arq: notify queue only (M16-004)
#   docker run <image> backup              # one encrypted pg_dump run
#   docker run <image> migrate             # alembic upgrade head
#   docker run <image> sh -c "..."         # anything else, verbatim
#
# Configuration comes from configuration.toml (see cloud_platform.core.config);
# CLOUD_PLATFORM_CONFIG_FILE only says where the file is. Environment variables
# and .env remain supported for bootstrap and tests. This script never needs
# to change for a deployment.
set -eu

case "${1:-api}" in
    api)
        shift || true
        exec uvicorn "cloud_platform.api.app:create_app" --factory \
            --host 0.0.0.0 --port "${PORT:-8000}" "$@"
        ;;
    bot)
        shift || true
        exec python -m cloud_platform.bot.main "$@"
        ;;
    worker)
        shift || true
        exec arq "cloud_platform.worker.settings.WorkerSettings" "$@"
        ;;
    worker-provisioning)
        shift || true
        exec arq "cloud_platform.worker.settings.ProvisioningWorkerSettings" "$@"
        ;;
    worker-billing)
        shift || true
        exec arq "cloud_platform.worker.settings.BillingWorkerSettings" "$@"
        ;;
    worker-notify)
        shift || true
        exec arq "cloud_platform.worker.settings.NotifyWorkerSettings" "$@"
        ;;
    backup)
        shift || true
        exec python -m cloud_platform.backup "$@"
        ;;
    migrate)
        shift || true
        # alembic/env.py reads DATABASE_URL from the environment and falls back
        # to configuration.toml, then strips the asyncpg prefix (migrations run
        # synchronously on psycopg2)
        exec alembic upgrade head "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
