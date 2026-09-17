FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY platform ./platform
COPY bb_platform ./bb_platform
COPY workers ./workers
RUN uv sync --frozen --no-dev --no-install-project
ENTRYPOINT ["/app/.venv/bin/python", "-m", "workers.modbus_tcp.main"]
