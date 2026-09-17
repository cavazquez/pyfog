.PHONY: setup run lint format typecheck test audit-dependencies audit-secrets audit check migrate dev-up dev-migrate dev-admin dev-down dev-clean \
	lan-migrate lan-admin lab-check agent-check pxe-check \
	image-manifest-check

setup:
	uv sync --frozen

migrate:
	uv run python -m pyfog init-db

run:
	uv run uvicorn pyfog.app:app --reload --host 127.0.0.1

dev-up:
	docker compose -f deploy/compose.dev.yaml up --build -d web

dev-migrate:
	docker compose -f deploy/compose.dev.yaml --profile admin run --rm migrate

dev-admin:
	docker compose -f deploy/compose.dev.yaml --profile admin run --rm migrate python -m pyfog create-admin --username "$${PYFOG_ADMIN_USERNAME:-admin}"

dev-down:
	docker compose -f deploy/compose.dev.yaml down

dev-clean:
	docker compose -f deploy/compose.dev.yaml down --remove-orphans -v

lan-migrate:
	docker compose -f deploy/compose.local-ca.yaml --profile admin run --rm migrate

lan-admin:
	docker compose -f deploy/compose.local-ca.yaml --profile admin run --rm migrate python -m pyfog create-admin --username "$${PYFOG_ADMIN_USERNAME:-admin}"

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

agent-check:
	bash -n agent/build-agent agent/init agent/pyfog-agent agent/udhcpc.script
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck agent/build-agent agent/init agent/pyfog-agent agent/udhcpc.script; fi
	agent/build-agent --help >/dev/null

pxe-check:
	bash -n pxe/build-pxe
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck pxe/build-pxe; fi
	pxe/build-pxe --help >/dev/null

image-manifest-check:
	python3 -m py_compile pyfog/image_manifest.py scripts/validate_image_manifest.py
	uv run python -m scripts.validate_image_manifest --help >/dev/null

check:
	./scripts/check.sh
