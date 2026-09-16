.PHONY: setup run lint format typecheck test audit-dependencies audit-secrets audit check migrate lab-check

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

audit-dependencies:
	uv run pip-audit --local --strict --progress-spinner off

audit-secrets:
	git ls-files -z | xargs -0 uv run detect-secrets-hook --no-verify # pragma: allowlist secret

audit: audit-dependencies audit-secrets

lab-check:
	bash -n lab/pyfog-lab

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy
	uv run pytest
	$(MAKE) lab-check
