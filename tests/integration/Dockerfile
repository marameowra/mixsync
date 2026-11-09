# The pytest runner for tests/integration: the locked dev dependencies; the repo is mounted at
# /repo read-only (see compose.yml), so the image only changes when uv.lock does.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

WORKDIR /deps
ENV UV_PROJECT_ENVIRONMENT=/venv PATH="/venv/bin:$PATH" PYTHONPATH=/repo/src \
    PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

WORKDIR /repo
ENTRYPOINT ["pytest", "-p", "no:cacheprovider", "-m", "integration", "tests/integration"]
