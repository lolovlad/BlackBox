FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY platform ./platform
COPY bb_platform ./bb_platform
COPY services ./services
COPY workers ./workers
COPY presets ./presets
COPY ui ./ui
COPY blackbox ./blackbox
COPY modbus_acquire ./modbus_acquire
RUN uv sync --frozen --no-dev
EXPOSE 8080
ENTRYPOINT ["/app/.venv/bin/uvicorn", "services.hub.app:app", "--host", "0.0.0.0", "--port", "8080"]
