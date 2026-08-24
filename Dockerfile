# syntax=docker/dockerfile:1
#
# The platform image (M12-002). Multi-stage, non-root, no package tooling
# in the runtime stage.
#
#   api      uvicorn HTTP service            (default; port 8000)
#   worker   arq job runner                  (provisioning, deletes, reconcilers)
#   backup   one encrypted pg_dump run
#   migrate  alembic upgrade head
#   anything else  executed verbatim (e.g. `sh`, `python -c ...`)
#
# Release (see scripts/release_oci_image.py): the image is tagged with the
# FULL git commit SHA - an immutable tag that is never re-pushed. Mutable
# tags (latest) are only ever produced by an explicit `--tag` flag.

# ---------------------------------------------------------------------------
# Build stage: install locked dependencies into a virtualenv.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

WORKDIR /app

# uv is the only installer; the lockfile is the single source of truth
# (the build fails if pyproject.toml and uv.lock drift, like CI does).
# First sync installs dependencies only (cached layer); after the sources
# are copied, the second sync adds the project itself (editable, pointing
# at /app/src).
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir "uv>=0.7,<1" \
    && uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python

COPY src ./src
RUN uv sync --frozen --no-dev --python /usr/local/bin/python

# ---------------------------------------------------------------------------
# Runtime stage: the virtualenv + the sources + the entrypoint, nothing else.
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# The editable project install in the venv points at /app/src, so the
# sources must live at exactly that path.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src ./src
COPY --chown=app:app docker-entrypoint.sh /app/docker-entrypoint.sh

USER app

EXPOSE 8000

# slim has no curl; the healthcheck uses the standard library against the
# liveness endpoint (readiness is for orchestrators that can check deps).
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status==200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["api"]