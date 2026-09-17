#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

if ! command -v uv >/dev/null 2>&1; then
  printf 'Error: no se encontró uv en PATH. Ejecutá la instalación del entorno primero.\n' >&2
  exit 1
fi

run_check() {
  local name="$1"
  shift
  printf '\n==> %s\n' "${name}"
  "$@"
}

check_dir="$(mktemp -d "${TMPDIR:-/tmp}/pyfog-check.XXXXXX")"
database_url="sqlite:///${check_dir}/pyfog.db"
cleanup() {
  rm -rf -- "${check_dir}"
}
trap cleanup EXIT

run_check "Lockfile" uv lock --check
run_check "Ruff lint" uv run ruff check .
run_check "Ruff format" uv run ruff format --check .
run_check "Mypy" uv run mypy
run_check "Python compile" uv run python -m compileall -q pyfog scripts migrations tests
run_check "Pytest" uv run pytest -q

run_check "Laboratorio shell" make lab-check
run_check "Agente shell" make agent-check
run_check "Perfil PXE" make pxe-check
run_check "Manifiesto de imagen" make image-manifest-check

run_check "Migraciones" env PYFOG_DATABASE_URL="${database_url}" uv run python -m pyfog init-db
run_check "Consistencia de migraciones" env PYFOG_DATABASE_URL="${database_url}" uv run alembic check
run_check "Auditoría de dependencias" uv run pip-audit --local --strict --progress-spinner off
run_check "Detección de secretos" bash -c \
  'git ls-files -z | xargs -0 uv run detect-secrets-hook --no-verify # pragma: allowlist secret'
run_check "Whitespace del working tree" git diff --check
run_check "Whitespace staged" git diff --cached --check

printf '\nTodos los checks pasaron.\n'
