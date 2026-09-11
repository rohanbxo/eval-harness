FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app/backend

# Dependencies first so edits to source don't bust the layer cache.
COPY backend/pyproject.toml backend/uv.lock* ./
RUN uv sync --frozen --no-install-project 2>/dev/null || uv sync --no-install-project

COPY backend/ ./
RUN uv sync --no-editable --no-dev 2>/dev/null || true

COPY config/ /app/config/
COPY scenarios/ /app/scenarios/

ENV PYTHONPATH=/app/backend \
    EVALHARNESS_SCENARIOS_DIR=/app/scenarios \
    EVALHARNESS_MODELS_FILE=/app/config/models.yaml

EXPOSE 8000
CMD ["uvicorn", "evalharness.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
