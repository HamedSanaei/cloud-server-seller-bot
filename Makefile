.PHONY: sync lint format-check typecheck test check api bot worker infra
sync:
	uv sync --all-groups
lint:
	uv run ruff check .
format-check:
	uv run ruff format --check .
typecheck:
	uv run mypy src
test:
	uv run pytest
check: lint format-check typecheck test
api:
	uv run uvicorn cloud_platform.api.app:create_app --factory --reload
bot:
	uv run python -m cloud_platform.bot.main
worker:
	uv run arq cloud_platform.worker.settings.WorkerSettings
infra:
	docker compose up -d postgres redis
