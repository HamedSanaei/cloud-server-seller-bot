# Staging deployment (M12-004)

Repeatable staging: the immutable release image (M12-002) + PostgreSQL +
Redis, with migrations applied BEFORE the api and worker start, backups
run on demand (M11-007), and state kept in named volumes so the same
commands redeploy the same environment.

## One-time setup

```bash
cd deploy/staging
cp .env.example .env       # then fill in the secrets
```

## Deploy (repeatable - run this for every release)

```bash
# 1) build + publish the immutable image from the release commit
uv run python scripts/release_oci_image.py --image registry.example.com/cloud-server-platform --push

# 2) point the environment at that image
echo "PLATFORM_IMAGE=registry.example.com/cloud-server-platform:$(git rev-parse HEAD)" >> .env

# 3) up - the migrate job runs alembic upgrade head first; api/worker wait
cd deploy/staging
docker compose up -d
```

## Verify

```bash
curl -fsS http://127.0.0.1:8000/health/live    # {"status":"ok"}
curl -fsS http://127.0.0.1:8000/health/ready    # {"status":"ok", ...}
curl -fsS http://127.0.0.1:8000/metrics | head  # prometheus output
docker compose ps                               # api/worker Up (healthy), migrate Exited (0)
docker compose logs worker | head               # arq started, jobs registered
```

## Backup / restore drill

```bash
docker compose --profile backup run --rm backup      # one encrypted dump
docker compose cp backup:/backups/cloud-backup-....dump.enc .   # fetch for the drill
```

The restore path is `docs/operations/RUNBOOK.md` → "Restore drill" (M11-008).

## Tear down / reset

```bash
docker compose down        # stop everything; volumes (state) are kept
docker compose down -v     # also delete state - a truly clean environment
```

Every deploy therefore runs the identical sequence: same image reference
model (SHA tag), same service graph, same migration-first ordering - which
is what makes the staging deploy repeatable.