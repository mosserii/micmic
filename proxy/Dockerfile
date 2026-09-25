# MicMic proxy for Railway (or any container host). Plain Dockerfile syntax: no BuildKit
# cache mounts, so it builds the same with any builder.
#
#   docker build -t micmic-proxy proxy/
#   docker run -p 8821:8080 -e PORT=8080 -e TYPESAFE_API_KEY=... -v micmic-data:/data micmic-proxy
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, from the lockfile exactly, so a code change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY proxy ./proxy
COPY web ./web

# Listen on every interface, on the port the host gives in PORT. The database lives on
# the volume mounted at /data, so it survives redeploys. Railway mounts volumes owned by
# root, which is why the process runs as root inside the container.
ENV PATH="/app/.venv/bin:$PATH" \
    MICMIC_PROXY_HOST=0.0.0.0 \
    MICMIC_PROXY_DB=/data/proxy.db \
    TRUST_PROXY_HEADERS=1

CMD ["python", "-m", "proxy"]
