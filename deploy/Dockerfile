# syntax=docker/dockerfile:1
FROM python:3.12.13-slim-bookworm AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY pyfog ./pyfog
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12.13-slim-bookworm

ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --system --gid 10001 pyfog \
    && useradd --system --uid 10001 --gid pyfog --home-dir /app pyfog \
    && mkdir --parents /app /var/lib/pyfog \
    && chown --recursive pyfog:pyfog /app /var/lib/pyfog
WORKDIR /app
COPY --from=build --chown=pyfog:pyfog /app /app

VOLUME ["/var/lib/pyfog"]
USER pyfog
EXPOSE 8000
CMD ["uvicorn", "pyfog.app:app", "--host", "0.0.0.0", "--port", "8000"]
