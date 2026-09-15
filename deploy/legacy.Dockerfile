FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY blackbox ./blackbox
COPY modbus_acquire ./modbus_acquire
COPY legacy ./legacy
RUN uv sync --frozen --no-dev

EXPOSE 5000

