#!/usr/bin/env bash
# Build deploy/seed.sql.gz: the four canonical runs, data only, in FK order.
#
# Three things this has to get right, each of which produced a corrupt dump
# before it was handled:
#
#  1. Order. pg_dump emits tables alphabetically, which would insert
#     assertion_results before the attempts they reference. Each table is
#     dumped separately in dependency order and concatenated.
#
#  2. Whole statements. Event payloads contain embedded newlines, so an INSERT
#     can span many lines. Filtering to lines matching ^INSERT silently keeps
#     only the first line of those statements and yields a dump that fails at
#     restore with "trailing junk after numeric literal". Nothing is filtered on
#     that basis: the dump is taken verbatim and only rewritten.
#
#  3. No psql meta-commands. pg_dump 16 wraps each dump in \restrict and
#     \unrestrict, which psql understands and asyncpg does not -- the loader is
#     asyncpg, and it fails the whole script with 'syntax error at or near "\"'.
set -euo pipefail

CONTAINER=evalharness-postgres-1
DB=evalharness
DBUSER=evalharness
RUNS="'45c1b19c','d0dbd7f2','a489b52b','a40a660a'"
OUT="$1"

BS='\'  # keeps the grep/sed patterns below readable

psql() { docker exec "$CONTAINER" psql -U "$DBUSER" -d "$DB" "$@"; }

echo "building filtered copies..." >&2
psql -q -c "
DROP SCHEMA IF EXISTS seedtmp CASCADE;
CREATE SCHEMA seedtmp;
CREATE TABLE seedtmp.runs              AS SELECT * FROM runs WHERE substr(id,1,8) IN ($RUNS);
CREATE TABLE seedtmp.attempts          AS SELECT a.*  FROM attempts a  JOIN seedtmp.runs r     ON a.run_id=r.id;
CREATE TABLE seedtmp.turns             AS SELECT t.*  FROM turns t     JOIN seedtmp.attempts a ON t.attempt_id=a.id;
CREATE TABLE seedtmp.events            AS SELECT e.*  FROM events e    JOIN seedtmp.attempts a ON e.attempt_id=a.id;
CREATE TABLE seedtmp.assertion_results AS SELECT ar.* FROM assertion_results ar JOIN seedtmp.attempts a ON ar.attempt_id=a.id;
" >/dev/null

TMP="$(mktemp)"
{
  echo "-- EvalHarness demo seed: the four-model run reported in docs/RESULTS.md."
  echo "-- Runs 45c1b19c gpt-5.6-terra, d0dbd7f2 claude-sonnet-5,"
  echo "--      a489b52b gemini-3.5-flash, a40a660a gpt-oss-120b-groq."
  echo "-- Data only; alembic owns the schema. Tables are ordered by foreign key"
  echo "-- dependency, not alphabetically, so this restores in one pass."
  echo "-- Loaded by deploy/seed.py; see docs/DEPLOY.md."
} > "$TMP"

for table in runs attempts turns events assertion_results; do
  echo "dumping $table..." >&2
  docker exec "$CONTAINER" pg_dump -U "$DBUSER" -d "$DB" \
    --data-only --column-inserts --no-owner --no-privileges \
    -t "seedtmp.$table" 2>/dev/null \
    | sed "s|^INSERT INTO seedtmp[.]|INSERT INTO public.|" \
    | sed "/^${BS}${BS}restrict /d; /^${BS}${BS}unrestrict /d" \
    >> "$TMP"
done

psql -q -c "DROP SCHEMA seedtmp CASCADE;" >/dev/null

fail() { echo "ERROR: $1" >&2; shift; "$@" >&2; rm -f "$TMP"; exit 1; }

# Refuse to ship a dump that still names the scratch schema.
if grep -q "seedtmp[.]" "$TMP"; then
  fail "seedtmp references survived the rewrite" grep -n "seedtmp[.]" "$TMP"
fi

# Refuse to ship a dump asyncpg cannot execute.
if grep -q "^[${BS}]" "$TMP"; then
  fail "psql meta-commands survived the filter" grep -n "^[${BS}]" "$TMP"
fi

# Refuse to ship a dump whose row counts do not match the source database.
for pair in "runs:4" "attempts:100" "turns:180" "events:3100" "assertion_results:660"; do
  table="${pair%%:*}"; want="${pair##*:}"
  got="$(grep -c "^INSERT INTO public[.]${table} " "$TMP" || true)"
  if [ "$got" != "$want" ]; then
    fail "expected $want rows for $table, dumped $got" true
  fi
done

gzip -9 -c "$TMP" > "$OUT"
rm -f "$TMP"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1)), all row counts match" >&2
