.PHONY: setup run lint format typecheck test check migrate

setup:
	uv sync --frozen

migrate:
	uv run python -m pyfog init-db

run:
	uv run uvicorn pyfog.app:app --reload --host 127.0.0.1

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest
