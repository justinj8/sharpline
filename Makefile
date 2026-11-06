.DEFAULT_GOAL := help
SHELL := /bin/bash

# Load .env when present so DATABASE_URL / TEST_DATABASE_URL reach every target.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: help fmt lint typecheck test test.unit test.integration check db.migrate dev worker cli plan

help: ## List targets
	@grep -E '^[a-zA-Z_.-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-18s %s\n", $$1, $$2}'

fmt: ## Format and autofix with ruff
	uv run ruff format .
	uv run ruff check --fix .

lint: ## ruff check + format check
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## mypy (strict)
	uv run mypy

test: ## Full test suite (integration tests skip without TEST_DATABASE_URL)
	uv run pytest

test.unit: ## Tests that need no database
	uv run pytest -m "not integration"

test.integration: ## Tests against TEST_DATABASE_URL
	uv run pytest -m integration

check: lint typecheck test ## lint + typecheck + test

db.migrate: ## alembic upgrade head against DATABASE_URL
	uv run alembic upgrade head

dev: ## Run the API with reload on :8000
	uv run uvicorn sharpline_api.app:create_app --factory --reload --port 8000

worker: ## Run the procrastinate worker
	uv run python -m sharpline_worker

cli: ## sharpline CLI with .env loaded, e.g. make cli ARGS="quota"
	uv run sharpline $(ARGS)

plan: ## Dry-run capture plan for a slate: make plan DATE=YYYY-MM-DD (ESPN schedule only)
	uv run sharpline capture nba --date $(DATE) --dry-run
