#!/bin/sh
# Image entrypoint (M12-002): one image, several entry points.
#
#   docker run <image> api                # uvicorn on :8000 (default)
#   docker run <image> worker              # arq job runner
#   docker run <image> backup              # one encrypted pg_dump run
#   docker run <image> migrate             # alembic upgrade head
#   docker run <image> sh -c "..."         # anything else, verbatim
#
# Configuration is environment-only (DATABASE_URL, REDIS_URL, ...); this
# script never needs to change for a deployment.
set -eu

case "${1:-api}" in
    api)
        shift || true
        exec uvicorn "cloud_platform.api.app:create_app" --factory \
            --host 0.0.0.0 --port "${PORT:-8000}" "$@"
        ;;
    worker)
        shift || true
        exec arq "cloud_platform.worker.settings.WorkerSettings" "$@"
        ;;
    backup)
        shift || true
        exec python -m cloud_platform.backup "$@"
        ;;
    migrate)
        shift || true
        # alembic/env.py reads DATABASE_URL and strips the asyncpg prefix
        # (migrations run synchronously on psycopg2)
        exec alembic upgrade head "$@"
        ;;
    *)
        exec "$@"
        ;;
esac