# EvalHarness

A config-driven evaluation harness that measures how well LLMs use tools in multi-turn
agentic workflows.

The same scenarios run against any model — only the model key changes. Grading is
deterministic: which tools were called, with what arguments, in what order, and whether
forbidden actions were avoided. Every scenario runs `k` times so you see *consistency*
(pass^k), not just a single lucky run.

```
scenarios/          what to test        (YAML + JSON, in git)
config/models.yaml  what to test it on  (a model registry)
backend/            the harness         (FastAPI + Celery + Postgres)
web/                the dashboard       (Next.js)
```

## Quick start

```bash
cp .env.example .env      # add at least one provider key
docker compose up --build
```

- API: <http://localhost:8000/api/health>
- Dashboard: <http://localhost:3000>

The dashboard's landing page is the leaderboard. Launch your first run at
`/runs/new`, or from the CLI (below).

## Running without Docker

```bash
cd backend
uv sync --all-groups
uv run evalharness list-models
uv run evalharness validate                       # loads every scenario, runs transcript tests
uv run evalharness run --model fake --scenarios all --k 1 --no-db
```

`--no-db` runs synchronously with no Postgres and no Celery and writes a results JSON.
It is what CI uses, and it is the fastest way to check a change.

## The CLI

```
evalharness validate [--scenario ID]      Load, schema-check, and run transcript tests
evalharness run --model KEY --scenarios all|id1,id2 --k 5 [--no-db] [--out results.json]
evalharness list-models
```

## Adding a model

Add an entry to `config/models.yaml` — that is the whole job.

```yaml
models:
  - key: my-model                        # stable key used in the UI and the CLI
    display_name: "Provider, Model X"
    litellm_model: "openai/some-model-id"   # any LiteLLM model string
    params: {temperature: 0}
    supports_parallel_tool_calls: true
    api_key_env: OPENAI_API_KEY
    pricing_override: null               # {input_per_mtok, output_per_mtok} if LiteLLM lacks pricing
```

The API key comes from the environment and nothing else. `GET /api/models` reports
whether each key is present; the run launcher disables models whose key is missing.
Cost is reported as `null` when pricing is unknown — the harness never guesses a price.

Models without native tool calling are marked unsupported and skipped.

## Adding a scenario

See **[docs/WRITING_SCENARIOS.md](docs/WRITING_SCENARIOS.md)** for the full guide. The
short version:

```
scenarios/my-scenario/
  scenario.yaml        turns, assertions, clock, system prompt
  tools.json           tool schemas + how each is mocked
  fixtures/*.json      canned tool responses
  transcripts/
    golden.yaml        ideal behavior — must pass, all axes at 100%
    fail_*.yaml        realistic failures — must fail on exactly the ids they declare
```

Then:

```bash
cd backend && uv run evalharness validate --scenario my-scenario
```

A scenario whose transcript tests do not pass is not done. Any change that affects
grading requires bumping the scenario's `version`.

## How grading works

Each attempt is one scenario run once. Assertions are tagged with an **axis**
(`selection`, `arguments`, `ordering`, `restraint`, `recovery`, `state`,
`clarification`, `safety`) and a **severity**:

| Severity | Effect |
|---|---|
| `critical` | Fails the whole attempt immediately — everything is still recorded |
| `required` | Must pass for the turn to pass |
| `soft` | Scored per axis; does not affect pass/fail |

An attempt passes when there are no critical failures and every `required` assertion in
every turn passed. Axis scores are passed ÷ total assertions on that axis.

Across a run of `k` repetitions:

- **pass@1** — mean attempt pass rate
- **pass^k** — fraction of scenarios where *all* `k` attempts passed

pass^k is the number to care about. A model that books the right flight four times out
of five is not a model you would ship.

## Results are reproducible

Every run stores a SHA-256 `config_hash` over the scenario files that produced it, the
git commit, the harness version, and a full JSON snapshot of each scenario. The
leaderboard only aggregates attempts sharing a `config_hash`; when a scenario changes,
the UI shows a "config changed" badge rather than silently mixing incomparable numbers.

`events` and `assertion_results` are append-only. Together they are the audit trail
behind every number in the dashboard.

## Development

```bash
cd backend
uv run ruff check . && uv run ruff format --check .
uv run mypy .                  # strict
uv run pytest --cov

cd ../web
npm run lint && npm run typecheck && npm run build
npm run gen:api                # regenerate API types from FastAPI's OpenAPI schema
```

Conventions live in [CLAUDE.md](CLAUDE.md); resolved spec ambiguities live in
[docs/DECISIONS.md](docs/DECISIONS.md).

**Tests never call a real model API.** The `fake` and `fake-b` model keys replay scripted
transcripts, which is how scenarios, the API, the worker and CI all exercise the full run
path — including the leaderboard and compare views, which need two models — with no
network and no keys.

## Scope

v1 mocks all tools — nothing here touches a live system. Scenarios are authored as files
in git, not through a UI. There is no auth and no multi-tenancy; it runs locally.
