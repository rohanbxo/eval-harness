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

## Decisions

Ambiguities resolved during the build are recorded in `docs/DECISIONS.md` with the
reasoning. Add to it rather than re-litigating a settled choice.
