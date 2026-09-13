# Deploying the read-only demo

The public demo is the dashboard over a frozen snapshot of one four-model run. It
runs three services on Railway — Postgres, the API, the dashboard — with **no
worker, no Redis, and no model API keys anywhere in the environment**.

Nothing in it can launch a run. `EVALHARNESS_READ_ONLY=true` makes the API answer
`POST /api/runs` and `POST /api/runs/{id}/cancel` with `405`, and
`NEXT_PUBLIC_READ_ONLY=true` hides the controls that would call them.

## What is in this directory

| file | what it does |
|---|---|
| `api.Dockerfile` | Production API image. Build context is the **repo root**. |
| `web.Dockerfile` | Production dashboard image. Build context is the **repo root**. |
| `entrypoint.sh` | API boot order: `alembic upgrade head` → seed → `uvicorn`. |
| `seed.py` | Loads the snapshot. Idempotent, and fails the boot if the database ends up empty. |
| `seed.sql.gz` | The snapshot: 4 runs, 100 attempts, 180 turns, 3100 events, 660 assertion results. |
| `make-seed.sh` | Regenerates `seed.sql.gz` from a local Postgres. |
| `verify.py` | Checks a deployment against `docs/RESULTS.md`. |

## Why no Redis

The API does not need it. `lifespan` opens only the database; the Celery and
Redis imports are all function-local. The progress event bus falls back to
`MemoryEventBus` whenever `REDIS_URL` is not a `redis://` URL, and `memory://` is
an explicitly supported value — so the demo sets `REDIS_URL=memory://` and runs
one fewer service. Live progress streaming does nothing useful on a deployment
that cannot start a run anyway.

## Railway setup

### 1. Project and database

New Project → **Deploy PostgreSQL**. Rename the service `Postgres`.

### 2. API service

**+ New → GitHub Repo →** this repository. Rename it `api`. Leave **Root
Directory empty** — `api.Dockerfile` copies `backend/`, `config/`, `scenarios/`
and `deploy/`, so the build context has to be the repo root.

Variables:

```
RAILWAY_DOCKERFILE_PATH = deploy/api.Dockerfile
DATABASE_URL            = ${{ Postgres.DATABASE_URL }}
REDIS_URL               = memory://
EVALHARNESS_READ_ONLY   = true
LOG_LEVEL               = INFO
```

`DATABASE_URL` arrives as `postgresql://`; `normalize_database_url` rewrites it to
`postgresql+asyncpg://`, so no driver suffix is needed here.

Then **Settings → Networking → Public Networking → Generate Domain**.

### 3. Dashboard service

**+ New → GitHub Repo →** the same repository. Rename it `web`. Root Directory
empty again. Variables:

```
RAILWAY_DOCKERFILE_PATH  = deploy/web.Dockerfile
NEXT_PUBLIC_API_BASE_URL = https://${{ api.RAILWAY_PUBLIC_DOMAIN }}
NEXT_PUBLIC_READ_ONLY    = true
```

**Generate Domain** for this service too.

`NEXT_PUBLIC_*` values are inlined into the client bundle at build time. Railway
passes every service variable as a Docker build argument, and `web.Dockerfile`
declares both with `ARG` in the build stage — but this does mean **changing
either one requires a redeploy, not just a restart**.

### 4. Let the API accept the dashboard's origin

On `api`, add:

```
EVALHARNESS_CORS_ORIGINS = https://${{ web.RAILWAY_PUBLIC_DOMAIN }}
```

Both domains must exist before these references resolve, which is why this step
is last. Redeploy `api`.

### 5. Check it

```
python deploy/verify.py https://<api-domain>
```

Exit code 0 means the leaderboard matches `docs/RESULTS.md` and the featured
trace renders.

## Boot behaviour

`entrypoint.sh` runs under `set -e`, so migrations and seeding must both succeed
before uvicorn starts. A failure takes the container down rather than serving an
empty dashboard that looks healthy.

`seed.py` logs exactly which path it took:

```
seed: seeded 4 run(s) from seed.sql.gz     # database was empty
seed: skipped, 4 run(s) already present    # redeploy, nothing written
seed: ok, 4 run(s) available               # post-condition, both paths
```

and exits non-zero if the database is empty afterwards, or if the snapshot
contains no `INSERT` statements at all. An empty database is a hard failure
precisely because it is the one outcome that would otherwise look like success.

## Regenerating the snapshot

`make-seed.sh` reads a local Postgres holding the canonical run and writes
`seed.sql.gz`. It refuses to write a dump that has the wrong row counts, that
still names its scratch schema, or that contains psql meta-commands.

```bash
bash deploy/make-seed.sh deploy/seed.sql.gz
```

Three things it has to get right, each of which produced a corrupt dump first:

- **Table order.** `pg_dump` emits tables alphabetically, which would insert
  `assertion_results` before the `attempts` they reference.
- **Whole statements.** Event payloads contain newlines, so an `INSERT` can span
  many lines. Filtering to lines matching `^INSERT` keeps only the first line of
  those and yields a dump that fails with `trailing junk after numeric literal`.
- **No psql meta-commands.** `pg_dump` 16 wraps output in `\restrict` and
  `\unrestrict`, which psql understands and asyncpg does not.

`seed.py` also resets `search_path` after loading: the dump carries pg_dump's own
`set_config('search_path', '', false)`, which otherwise makes every later
unqualified table name fail to resolve.

## Cost

Roughly **$9.50/month** of usage at Railway's published per-second rates
(`$20.01/vCPU-month`, `$10.01/GB-month`, `$0.156/GB-month` volume, `$0.05/GB`
egress), assuming ~250–300 MB resident per service and light traffic. The Hobby
plan's $5 subscription includes $5 of that. Memory dominates, so the figure moves
with resident set size more than with traffic.
