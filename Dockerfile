# syntax=docker/dockerfile:1
# YouGile MCP Cloud: one image, configured entirely by environment variables (see README).

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.14-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first: this layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.14-slim
LABEL org.opencontainers.image.source="https://github.com/indalo-tech/yougile-mcp-cloud" \
      org.opencontainers.image.description="Hosted YouGile MCP: sign in with YouGile, per-company limits and rights" \
      org.opencontainers.image.licenses="AGPL-3.0-only"
# The system CA store is what the app trusts (httpx2 via truststore). The October 2026 eMudhra
# intermediate CA compromise is fixed in ca-certificates 20250419+deb13u1: take the newest
# package, and refuse to build an image that would still trust those intermediates.
ARG CA_MIN=20250419+deb13u1
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && v="$(dpkg-query -W -f='${Version}' ca-certificates)" \
    && dpkg --compare-versions "$v" ge "$CA_MIN" \
    || { echo "ca-certificates ${v:-?} is older than $CA_MIN" >&2; exit 1; }
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000
USER app
WORKDIR /app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["yougile-cloud", "serve"]
