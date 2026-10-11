FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends libchromaprint-tools \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"

# Deps first so source edits don't bust this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Compose overrides the uid/gid via `user:` (PUID/PGID); this is the fallback.
RUN useradd --uid 1000 --create-home mixsync \
 && mkdir /config /downloads /data && chown mixsync /config /downloads /data
USER mixsync

EXPOSE 8080
ENTRYPOINT ["mixsync"]
CMD ["web"]
