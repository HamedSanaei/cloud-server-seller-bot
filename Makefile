.PHONY: sync lint format-check typecheck test check staging-check staging-test pre-commit api bot worker infra migrate

# Sync dependencies including dev tools
sync:
	uv sync --all-groups

# Lint with ruff
lint:
	uv run ruff check .

# Check formatting with ruff
format-check:
	uv run ruff format --check .

# Static type checking
typecheck:
	uv run mypy src

# Run unit tests
test:
	uv run pytest

# Run all quality gates (lint, format, typecheck, test, task validation)
check: lint format-check typecheck test
	uv run python scripts/validate_tasks.py

# Fast staging-lane checks: exactly what .github/workflows/deploy-staging.yml
# runs before building (ruff check, ruff format --check, compileall over src,
# import smoke of api/worker/bot). No pytest, no coverage, no mypy: the
# release lane (`make check`, `verify_ci.py --push-ready`) keeps those.
staging-check:
	uv run python scripts/verify_ci.py --staging

# The complete staging-lane validation in one command: ONLY the tests you name,
# then the fast staging gate. No full suite, no coverage, no mypy, no Docker.
# Usage: make staging-test TESTS="tests/unit/test_business_log.py tests/unit/test_hourly_state_machine.py"
staging-test:
	uv run python scripts/verify_ci.py --staging $(addprefix --test ,$(TESTS))

# Run pre-commit hooks on all files
pre-commit:
	uv run pre-commit run --all-files

# Run database migrations (usage: make migrate upgrade)
migrate:
	uv run alembic $(RUN)

# Run API server (dev)
api:
	uv run uvicorn cloud_platform.api.app:create_app --factory --reload

# Run Telegram bot
bot:
	uv run python -m cloud_platform.bot.main

# Run background worker
worker:
	uv run arq cloud_platform.worker.settings.WorkerSettings

# Start infrastructure (PostgreSQL + Redis)
infra:
	docker compose up -d postgres redis
