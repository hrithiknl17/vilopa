export VILOPA_APP_PG_DSN ?= postgresql://vilopa:vilopa@127.0.0.1:55432/bharat_bazaar
COMPOSE := docker compose -f deploy/compose.dev.yml

.PHONY: dev down seed test-fast test-db lint fmt types schema check

dev:  ## install dependencies and start Postgres
	uv sync
	$(COMPOSE) up -d --wait

down:
	$(COMPOSE) down

seed:  ## load the demo shop into the dev database
	uv run python examples/bharat-bazaar/seed.py

test-fast:  ## unit tests, no database
	uv run pytest -m "not db"

test-db:  ## integration tests against the dev database
	uv run pytest -m db

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

types:
	uv run mypy

schema:  ## regenerate the committed data map JSON Schema
	uv run vilopa datamap schema > schemas/datamap.schema.json

check: lint types test-fast
