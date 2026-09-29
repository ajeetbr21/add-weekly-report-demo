# One lightweight image for api, worker, mcp-local, telegram and migrate services.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml ./
# install dependencies first for layer caching
RUN mkdir app && touch app/__init__.py && pip install . && rm -rf app

COPY backend/ ./
RUN useradd --create-home --uid 10001 atlas \
    && mkdir -p /workspace && chown atlas:atlas /workspace

USER atlas
EXPOSE 8000 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
