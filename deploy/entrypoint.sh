#!/bin/sh
# Boot order for the demo API: schema, then data, then serve.
#
# `set -e` is load-bearing, not habit. Without it a failed migration or a failed
# seed would fall through to uvicorn and the dashboard would come up serving an
# empty database -- a deployment that looks healthy and shows nothing. Every step
# here must succeed before the next one starts, and any failure takes the
# container down where Railway will show it.
set -e

# Name the host before connecting: a bad DATABASE_URL otherwise surfaces only as
# a DNS error deep in a traceback that never says which name failed to resolve.
python -c "from evalharness.db.session import describe_database_target, resolve_database_url; print('entrypoint:', describe_database_target(resolve_database_url()))"

echo "entrypoint: applying migrations"
alembic upgrade head

echo "entrypoint: seeding the demo snapshot"
python /app/deploy/seed.py

echo "entrypoint: starting uvicorn on port ${PORT:-8000}"
exec uvicorn evalharness.api.app:app --host 0.0.0.0 --port "${PORT:-8000}"
