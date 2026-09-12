# EvalHarness — working conventions

A config-driven harness that measures how well LLMs use tools in multi-turn agentic
workflows. Scenarios are files in git; everything a run produces goes to Postgres.
`SPEC.md` is the source of truth — read it before changing behavior.

## Layout

```
config/models.yaml          model registry (SPEC 7)
scenarios/<id>/             scenario.yaml, tools.json, fixtures/, transcripts/
backend/evalharness/
  schema/   Pydantic models for all config + runtime records — the shared vocabulary
  loader/   load + validate scenarios, compute config hash
  engine/   matchers, mock tool engine, faults, handlers
  runner/   conversation loop, provider adapters (LiteLLM, FakeModel)
  grader/   assertion evaluation, scoring
  db/       SQLAlchemy models, Alembic migrations
  api/      FastAPI routes
  worker/   Celery app + tasks
  cli.py    headless CLI
web/                        Next.js dashboard
```

## Python

- Python 3.12. `uv` for dependency management; run tools via `uv run`.
- Fully type-hinted. `uv run mypy .` must be clean under `--strict` (configured in
  `pyproject.toml`, so plain `mypy .` is strict).
- `uv run ruff check .` and `uv run ruff format --check .` must be clean.
- Async throughout the API and runner. The engine, matchers and grader are sync and pure.
- **Pydantic models are the only way config enters the system.** No raw dict access to
  scenario data outside `loader/`. All config models use `extra="forbid"` — unknown keys
  are a loud error, never a silent ignore.
- Matchers, engine and grader are pure and deterministic: no network, no `datetime.now()`.
  Time comes from the scenario `clock`.

## Tests

- pytest, `asyncio_mode = "auto"`. Run with `uv run pytest`.
- **Never call a real model API in a test.** Use `FakeModel` (SPEC 6.4). This is absolute.
- Matchers and grader aim for near-100% branch coverage, including failure *reasons* —
  a matcher that fails must explain itself well enough to render in the trace viewer.
- Scenario correctness is tested through its transcripts: `golden.yaml` must pass with
  all axes at 1.0, and every `fail_*.yaml` must fail on exactly its `expect_failures` ids.

## Scenarios

- All content is fictional: invented companies, people, `*.test` domains, invented IDs.
  Fixture page text is original prose written for this project.
- Any change to a scenario that affects grading requires bumping its `version` and a
  passing `evalharness validate`.
- `mock` config lives inside `tools.json` but is stripped before tools reach the model.

## Frontend

- TypeScript strict, no `any`. Server components by default; client components only where
  the page is actually interactive.
- API types are generated from FastAPI's OpenAPI schema: `npm run gen:api`. Do not
  hand-write response types.
- Dark and light mode both work. Loading and empty states everywhere.
- The database is the source of truth — never persist anything meaningful client-side.

## Secrets

Environment variables only. `.env` is gitignored; document every key in `.env.example`.
Never log an API key, and never put one in a fixture or a test.

## Reporting results

**Every summary claim in a report must be computed from the results data, not written
freehand. Include the query or computation that backs it.**

"No model failed the same scenario twice" is a claim about counts; it must come from
counting, and the count must be shown. A run of this harness exists to replace impressions
with measurements, so a report that reintroduces impressions defeats it. This is not a
style preference — it was added after a report asserted exactly that sentence while the
table printed directly above it showed a model failing one scenario twice.

Practically:

- Derive per-model and per-scenario numbers with a script over `results/*.json` or the
  database, and paste the script or query alongside the finding.
- Prefer a printed table to a sentence. Where a sentence generalises over a table, it must
  be produced by the same computation, not read off by eye.
- Say which source the numbers came from. A `--no-db` run writes JSON and stores nothing
  in Postgres; claiming to have queried the database in that case is false.
- If a claim cannot be computed, do not make it. "Three of five scenarios never executed"
  is checkable; "the model seemed cautious" is not, unless it is quoting a specific
  message, in which case quote it.

## Guards need a negative test

**Every guard, check or assertion needs a test proving it can fail.** Not a test that it
passes when it should — a test that it *fails* when the thing it guards against is present.

This is the single most expensive lesson in the project. Of seven harness defects found,
four were the same shape: a guard that could not fire, reporting success.

- The token-bucket rate limiter passed its own unit tests while admitting twice its rate,
  because the tests asserted the bucket's internal arithmetic rather than the thing a
  provider measures.
- The cross-process limiter test was `skipif`-guarded on an environment variable nothing
  in CI set, so the only test that could catch a per-process window had never run.
- `is_dirty()` returned `False` inside a container because `git` was absent, so the
  dirty-tree guard reported "clean" for every comparison run ever made.
- `args_not_contains` returned "passed" when its tool was never called, so a leak check
  that examined nothing looked identical to a leak check that found nothing.

In each case the green result was produced by the check *not running*, and nothing in the
output distinguished that from the check passing.

The pattern to follow is already in the suite:

- `tests/test_ratelimit_window.py::test_a_token_bucket_fails_this_suite` keeps the replaced
  token bucket alive purely to prove the invariant test catches it. If that test ever
  passes, the invariant test has stopped testing anything.
- `tests/test_turn_budget.py::test_the_old_wrapping_fails_this_suite` reconstructs the
  previous `wait_for` arrangement and asserts the turn dies, so the guarantee cannot
  silently stop being tested.

Write the equivalent for anything that guards: keep the broken implementation, or
construct the input the guard exists to reject, and assert it is rejected. Where a check
depends on its environment (a service, a binary, a file on disk), CI must provide that
environment and fail if the test skipped — a skip-guarded test with nothing satisfying the
guard is indistinguishable from no test at all.

Distinguish *evidence of absence* from *absence of evidence* in the result, too: a check
that could not run reports `not_evaluable`, never a pass.

## Decisions

Ambiguities resolved during the build are recorded in `docs/DECISIONS.md` with the
reasoning. Add to it rather than re-litigating a settled choice.
