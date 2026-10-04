.PHONY: help install lint format typecheck test test-unit test-integration check db-up db-down migrate migration

help:
	@echo Targets: install lint format typecheck test test-unit test-integration check db-up db-down migrate migration

install:
	python -m pip install --upgrade pip
	python -m pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

typecheck:
	mypy

test:
	pytest

test-unit:
	pytest -m "not integration"

test-integration:
	pytest -m integration

# Everything CI runs, in the same order.
check: lint typecheck test

db-up:
	docker compose up -d

db-down:
	docker compose down

# Apply all migrations to the database in AIDE_DATABASE_URL (.env).
migrate:
	alembic upgrade head

# Generate a new migration from model changes: make migration m="add foo"
migration:
	alembic revision --autogenerate -m "$(m)"
