.PHONY: install test lint format migrate run-ingestion

install:
	pip install -e ".[dev]"

test:
	pytest tests/ -v

lint:
	ruff check src/ tests/
	mypy src/ tests/

format:
	ruff format src/ tests/

migrate:
	alembic upgrade head

run-ingestion:
	python -m scripts.run_ingestion
