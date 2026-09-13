# Production image for the public read-only demo's API.
#
# The build context is the REPO ROOT, not backend/: this image needs backend/,
# config/ and scenarios/, plus the seed snapshot in deploy/. On Railway that
# means leaving "Root Directory" empty and pointing the service at this file with
# RAILWAY_DOCKERFILE_PATH=deploy/api.Dockerfile.
#
# Differs from backend/Dockerfile, which is the local compose image: this one
# carries the seed and runs migrations and seeding before serving.
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
COPY deploy/seed.py deploy/seed.sql.gz deploy/entrypoint.sh /app/deploy/
RUN chmod +x /app/deploy/entrypoint.sh

# The commit this image was built from. A container has no .git and no git
# binary, so this is the only way a run can identify its own code (D41). The
# demo never launches a run, but an image that cannot name its commit is worth
# avoiding anyway.
ARG GIT_COMMIT=""

ENV PYTHONPATH=/app/backend \
    EVALHARNESS_SCENARIOS_DIR=/app/scenarios \
    EVALHARNESS_MODELS_FILE=/app/config/models.yaml \
    EVALHARNESS_GIT_COMMIT=${GIT_COMMIT} \
    PORT=8000

EXPOSE 8000
CMD ["/app/deploy/entrypoint.sh"]
