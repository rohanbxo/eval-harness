"""Load the demo seed into the deployment's Postgres, and say what it did.

Runs on every boot of the public demo, before uvicorn. Three outcomes, and the
log line always distinguishes them:

* ``seeded``  -- the database was empty and the snapshot was loaded.
* ``skipped`` -- runs were already present, so nothing was written.
* ``failed``  -- anything else. The process exits non-zero and the container
  does not come up.

The post-condition is the point. A seed that silently no-ops leaves an empty
dashboard that looks like a working deployment with nothing to show, which is
exactly the shape of failure this project keeps finding: a step that did not run
being indistinguishable from a step that succeeded (DECISIONS D38, D46). So the
run count is re-read *after* seeding and an empty database is a hard failure,
whichever path was taken to get there.
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import sys
from pathlib import Path

import asyncpg

LOGGER = logging.getLogger("evalharness.seed")

SEED_FILE = Path(__file__).resolve().parent / "seed.sql.gz"

#: One arbitrary but fixed key, so two containers booting together cannot both
#: decide the database is empty and load the snapshot twice.
ADVISORY_LOCK_KEY = 0x5EED_0001


def dsn_from_env() -> str:
    """asyncpg wants a plain URL; the app's own setting carries a driver suffix."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("seed: DATABASE_URL is not set")
    for prefix in ("postgresql+asyncpg://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


async def count_runs(connection: asyncpg.Connection) -> int:
    """Run count, treating a missing table as zero rather than an error.

    Migrations run before this script, so the table should exist; if it does not,
    the post-condition check below is what turns that into a visible failure.
    """
    exists = await connection.fetchval("SELECT to_regclass('public.runs') IS NOT NULL")
    if not exists:
        return 0
    # Schema-qualified on purpose. The dump carries pg_dump's own
    # `set_config('search_path', '', false)`, so after loading it an unqualified
    # name does not resolve and this count fails with UndefinedTableError on a
    # database that was in fact seeded correctly.
    result = await connection.fetchval("SELECT count(*) FROM public.runs")
    return int(result or 0)


async def seed() -> None:
    if not SEED_FILE.is_file():
        raise SystemExit(f"seed: snapshot missing at {SEED_FILE}")

    connection = await asyncpg.connect(dsn_from_env())
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
        try:
            before = await count_runs(connection)
            if before > 0:
                LOGGER.info("seed: skipped, %d run(s) already present", before)
            else:
                script = gzip.decompress(SEED_FILE.read_bytes()).decode("utf-8")
                if "INSERT INTO" not in script:
                    raise SystemExit(
                        f"seed: {SEED_FILE.name} contains no INSERT statements. A "
                        "truncated or corrupt snapshot would otherwise load cleanly "
                        "and leave an empty dashboard."
                    )
                # asyncpg's simple protocol runs the whole multi-statement script
                # in one round trip. Wrapped in a transaction here rather than in
                # the dump, so a failure half way leaves no partial snapshot for
                # the next boot to mistake for a complete one.
                async with connection.transaction():
                    await connection.execute(script)
                # Undo the dump's search_path reset for the rest of this session.
                await connection.execute("SET search_path TO public")
                seeded = await count_runs(connection)
                LOGGER.info("seed: seeded %d run(s) from %s", seeded, SEED_FILE.name)

            after = await count_runs(connection)
            if after == 0:
                raise SystemExit(
                    "seed: the database is empty after seeding. The dashboard would "
                    "come up with nothing in it, so this boot is being failed instead. "
                    f"Checked table public.runs via {SEED_FILE.name}."
                )
            LOGGER.info("seed: ok, %d run(s) available", after)
        finally:
            await connection.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)
    finally:
        await connection.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(seed())


if __name__ == "__main__":
    main()
