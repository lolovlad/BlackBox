# syntax=docker/dockerfile:1
#
# Dependency layers are independent of application source. A code-only update
# reuses the cached venv; set BB_PULL=1 to refresh python:3.12-slim.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
RUN pip install --no-cache-dir uv

FROM base AS hub-deps
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_COMPILE_BYTECODE=1 uv sync --frozen --no-dev --no-install-project

FROM hub-deps AS hub
COPY platform ./platform
COPY bb_platform ./bb_platform
COPY services ./services
COPY workers ./workers
COPY presets ./presets
COPY ui ./ui
COPY blackbox ./blackbox
COPY modbus_acquire ./modbus_acquire
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/uvicorn", "services.hub.app:app", "--host", "0.0.0.0", "--port", "8080"]

FROM base AS worker
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=deploy/worker-requirements.txt,target=/tmp/worker-requirements.txt \
    uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python -r /tmp/worker-requirements.txt
COPY platform ./platform
COPY bb_platform ./bb_platform
COPY workers ./workers

FROM worker AS worker-simulator
ENTRYPOINT ["/app/.venv/bin/python", "-m", "workers.simulator.main"]

FROM worker AS worker-rtu
ENTRYPOINT ["/app/.venv/bin/python", "-m", "workers.modbus_rtu.main"]

FROM worker AS worker-tcp
ENTRYPOINT ["/app/.venv/bin/python", "-m", "workers.modbus_tcp.main"]

FROM hub-deps AS legacy
COPY blackbox ./blackbox
COPY modbus_acquire ./modbus_acquire
COPY legacy ./legacy
EXPOSE 5000
