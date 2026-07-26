ARG BUILD_FROM=python:3.13-slim-bookworm
FROM ${BUILD_FROM}

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY DOCS.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

CMD ["/app/.venv/bin/python", "-m", "numbat"]
