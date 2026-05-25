# syntax=docker/dockerfile:1.7
ARG BACKEND_BASE_IMAGE=localhost/web_terminal_acp-backend-base:local
FROM ${BACKEND_BASE_IMAGE}

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,id=web-terminal-acp-uv,target=/root/.cache/uv \
    HOME=/root uv sync --frozen --no-dev --no-install-project

COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser migrations ./migrations
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser --chmod=755 docker-entrypoint.sh ./

USER appuser
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
