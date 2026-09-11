# Decisions

Ambiguities in `SPEC.md` resolved during the build, with reasoning. Append; don't rewrite.

## D1 — Fixture responses carry an explicit `ok` flag

**Spec:** §5.2 shows fixture rows returning a value verbatim, and a `default` that looks
like an error (`{"error": "not_found"}`), but never says how the engine decides whether a
result is a success or a failure.

**Decision:** `FixtureResponse.ok` defaults to `true` and `FixtureFile.default_ok` defaults
to `false`. So a matched row is a success unless it says otherwise, and falling through to
`default` is an error unless the author says otherwise.

**Why:** the `recovered` assertion (§4.3) depends on distinguishing an errored call from a
successful one, and sniffing for an `"error"` key in the payload would make a fixture that
legitimately returns a field named `error` behave surprisingly. An explicit flag keeps the
engine's success/failure decision declarative.

## D2 — `Matcher` allows `gte` + `lte` together

**Spec:** §4.4 lists `gte` and `lte` as separate matchers.

**Decision:** exactly one primary matcher key per matcher, except that `gte` and `lte` may
be combined to express a closed numeric range.

**Why:** a bounded range is the common case for numeric arguments, and the alternative
(inventing a `between` matcher, or nesting an `all_of`) adds surface area for no gain.

## D3 — Scenario clock must carry a UTC offset

**Spec:** §4.1 shows `clock: "2026-03-02T09:00:00+04:00"` but does not require the offset.

**Decision:** reject a naive clock at load time.

**Why:** `meeting-scheduler` grades `datetime_equals` against `+04:00` instants. A naive
clock would make the correct answer depend on the host's timezone, which breaks the
determinism guarantee in §11.

## D4 — Assertion ids are unique per scenario, not per turn

**Spec:** §4.3 says "unique within the scenario".

**Decision:** enforced in `Scenario` validation across all turns, and the error names both
turns involved.

**Why:** `assertion_results` rows are keyed by `(attempt_id, assertion_id)` in reports and
in the compare view's "assertions that flipped" logic; duplicates would silently merge.

## D5 — `fake` is a first-class registry entry

**Spec:** §6.4 describes `FakeModel` as a provider adapter; §7 describes the registry.

**Decision:** `config/models.yaml` ships a `fake` model key whose provider resolves to
`FakeModel`, with `api_key_env: null` and zero pricing.

**Why:** it lets the API, the worker, the dashboard and CI all exercise the full run path
(`POST /runs` → Celery → persistence → SSE) with no network and no keys, which Phase 4's
acceptance criteria require.

## D6 — Handlers resolve paths through a reserved `_scenario_dir` config key

**Spec:** §5.3 says a handler receives `(args, state, config)` and that `sqlite_query` is
seeded "from a `.sql` file", without saying how a path in `config` is resolved.

**Decision:** paths in `mock.config` are relative to the scenario directory, and the engine
injects that directory into the config it passes as the reserved key `_scenario_dir`
(`engine.handlers.SCENARIO_DIR_KEY`). Absolute paths are used as-is. The handler signature
stays exactly `(args, state, config)`.

**Why:** the alternative — rewriting `config["seed"]` into an absolute path before the call —
only helps handlers whose file key is named `seed`. A single reserved key lets any future
handler resolve any number of files, keeps `mock.config` in `tools.json` readable and
relocatable, and keeps handlers free of a dependency on the loader.

## D7 — The per-tool call counter counts every call, including rejected ones

**Spec:** §5.4 defines `on_call` as "1-indexed call count for this tool within the attempt"
but does not say whether a call that fails schema validation, or one that is itself faulted,
advances that count.

**Decision:** the counter advances for every call the model makes to a *known* tool,
including calls rejected as `invalid_arguments` and calls that a fault answered. A
hallucinated tool name does not advance any counter, because there is no tool to count for.

**Why:** `on_call` scripts the model's experience ("the second time you reach for this tool,
it times out"), and from the model's side a rejected call is a call it made and saw fail.
Counting only successful dispatches would make fault timing depend on how well the model
formats arguments, which is exactly the behavior being graded — the fault would move around
between models and make runs incomparable.

## D8 — SQLite read-only enforcement uses the authorizer, not a keyword blocklist

**Spec:** §5.3 says `sqlite_query` "rejects anything that is not a single `SELECT` or
`WITH ... SELECT`".

**Decision:** three independent layers. (1) The SQL text is masked (comments and string /
identifier literals blanked out) before it is split on semicolons, so more than one
statement is refused and `SELECT * FROM t; DROP TABLE t` cannot slip through. (2) The
leading keyword must be `SELECT`, `WITH` or `VALUES`. (3) During execution a
`sqlite3` authorizer callback denies every action except `SELECT`, `READ`, `FUNCTION` and
`RECURSIVE`, and the connection is pinned with `PRAGMA query_only = ON`.

**Why:** layers (1) and (2) are text inspection and would pass `WITH x AS (SELECT ...)
DELETE FROM ...`, which SQLite accepts. The authorizer runs inside SQLite's own statement
preparation, so it is the only check that cannot be talked around by clever text. The
`ok=False` result the model sees is identical whichever layer refuses.

## D9 — Matcher failure reasons quote only the expectation and the value

**Spec:** §4.4 gives one example reason: `destination: expected one of [RUH, Riyadh], got
"Jeddah"` — for an `any_of` matcher that also carried `ci: true`.

**Decision:** reasons render the expectation and the observed value and nothing else; the
`ci` and `partial` modifiers are not annotated in the text. Observed values are rendered as
JSON (`got "Jeddah"`, `got 5`, `got null`), expectation lists render strings bare
(`[RUH, Riyadh]`). When several matchers in one `args` block fail, every reason is reported,
joined with `"; "`.

**Why:** the spec's example is a literal string the trace viewer is built around, and it
omits the modifier. A mismatch under `ci` is a real mismatch either way, so the annotation
would add noise to every reason to explain one rare case.

## D10 — The SQLite handler config key is `seed_file`

**Context:** the engine and the scenarios were built in parallel; the handler read
`config["seed"]` while `data-analyst` and `docs/WRITING_SCENARIOS.md` both wrote
`seed_file`. Every `run_sql` call failed with `database_unavailable`, which the fault
injected on call 1 disguised as a plausible timeout.

**Decision:** `seed_file` is the name, with no alias for `seed`.

**Why:** it is the name already documented for scenario authors, and it says what the
value is. An alias would have let the two spellings drift apart again; a single name
fails loudly at load time instead.

## D11 — The SSE route declares its schema with `model`, not a hand-written `$ref`

**Context:** `GET /runs/{id}/stream` described its `text/event-stream` body with
`{"$ref": "#/components/schemas/ProgressEvent"}`. Because no route *returns*
`ProgressEvent`, FastAPI never emitted it into `components/schemas`, so the OpenAPI
document contained a dangling reference and `openapi-typescript` refused to generate.

**Decision:** declare the response with FastAPI's `"model": schemas.ProgressEvent`.

**Why:** it makes FastAPI register the component, so the schema stays internally
consistent. A dangling `$ref` is invalid OpenAPI, and nothing in the app would have
noticed until type generation ran.

## D12 — `web/lib/api-types.ts` is generated *and* committed

**Spec:** §2 requires frontend API types to be generated from the OpenAPI schema.

**Decision:** `npm run gen:api` writes `lib/api-types.ts`, and that file is committed.
`lib/types.ts` aliases it into the names the app uses; `lib/vocabulary.ts` holds only
the enum lists the UI needs at *runtime*, with compile-time assertions that they still
match the generated unions. A CI job regenerates both and fails on any diff.

**Why:** the web build imports these types, so CI would otherwise need a running API and
a Python toolchain just to typecheck. Committing the artifact keeps the web job
self-contained, and the drift check preserves the actual guarantee — that nobody
hand-edits a response type.

`gen:api` resolves its schema from a running API first and a dumped `openapi.json`
second, so a fresh checkout can regenerate without standing up the stack. It also runs
`openapi-typescript` entirely inside a temp directory: the library URL-encodes paths and
then stats them literally, which fails on any path containing a space.

## D13 — Compare reports assertion *pass rates* and a direction, not two booleans

**Spec:** §9.2 asks compare to "highlight assertions that flipped between the runs".

**Decision:** each flip carries `pass_rate_a`, `pass_rate_b` and a `direction` of
`fixed` / `broken` / `improved` / `regressed` / `added` / `removed`, rather than a
`passed_a`/`passed_b` pair.

**Why:** a run is k attempts, so an assertion can pass 2 of 3 times. Collapsing that to
one boolean would report a model that got flakier as unchanged, which is exactly the
regression pass^k exists to expose. `fixed` and `broken` are reserved for clean 0→1 and
1→0 moves so the strong claim stays distinguishable from a shift in flakiness.

## D14 — `grader.judge` defers its `Provider` import

**Context:** `evalharness.grader.judge` imported `Provider` from `evalharness.runner.provider`,
but importing anything under `evalharness.runner` runs that package's `__init__`, which
imports the conversation loop, which imports `grader.judge`. `import
evalharness.grader.judge` therefore failed with a circular-import error. Nothing noticed,
because every existing caller imported `runner` first.

**Decision:** `Provider` is imported under `TYPE_CHECKING` in `judge.py`.

**Why:** it is used there only as an annotation, and `from __future__ import annotations`
is already in force, so nothing is needed at runtime. The alternative — importing the
grader lazily inside the conversation loop — would hide the cycle rather than remove it.

## D15 — `.env.example` does not set the scenario or registry paths

**Context:** the example env file set `EVALHARNESS_SCENARIOS_DIR=/app/scenarios` and
`EVALHARNESS_MODELS_FILE=/app/config/models.yaml`. Anyone following the README's
`cp .env.example .env` then got a CLI that looked for scenarios at a path which only
exists inside a container: `evalharness validate` reported "no scenarios directory".

**Decision:** both variables are left unset in `.env.example`, with a comment saying why.

**Why:** `Settings` already defaults them to `./scenarios` and `./config/models.yaml`
relative to the repo root, and `docker-compose.yml` sets the container paths explicitly on
the `api` and `worker` services. Setting them a third time in `.env` only created a way to
get it wrong.

## D16 — A second scripted model key, `fake-b`

**Spec:** Phase 7's acceptance asks for the leaderboard and compare views exercised with
"FakeModel runs seeded for two fake model keys", including a config-changed case.

**Decision:** the registry ships `fake-b` alongside `fake`, identical except for its
display name.

**Why:** the leaderboard is a models × scenarios matrix and the compare view diffs two
runs, so neither can be meaningfully tested — or demonstrated — with one model. Both keys
are keyless and free, so the whole comparison path stays exercisable with no provider
account. The transcript a run replays is chosen per run, not by model key, so one extra
entry is enough to produce a strong and a weak row.

## D17 — Compose publishes host ports through overridable variables

**Context:** the first `docker compose up` on the dev machine failed: another stack was
already bound to 5432, 6379 and 3000.

**Decision:** every published port is `${POSTGRES_PORT:-5432}` and friends, documented in
`.env.example`. The defaults are unchanged, so the README still works as written.

**Why:** the services reach each other over the compose network regardless; the published
ports exist only for the host. Making them overridable costs nothing and removes the most
likely first-run failure on a machine that already runs something else.

## D18 — `apiFetch` casts without validating, so fetchers must unwrap deliberately

**Context:** `getScenarios` was declared as returning `ScenarioListItem[]` while
`/api/scenarios` returns `{"scenarios": [...]}`. It typechecked and built cleanly, then
threw `result.data.map is not a function` at render. `getModels` had the identical bug.
Both were found only by loading the pages against a running API.

**Decision:** endpoints that wrap their payload unwrap it in `lib/api.ts` (`getScenarios`,
`getModels`), and the generated types name the envelope explicitly
(`ScenarioListResponse`, `ModelListResponse`) so the unwrap is visible at the call site.

**Why the bug was possible at all:** `apiFetch<T>` asserts `T` over `response.json()`
without checking it. Generated types make the *shapes* correct but cannot make a call site
pick the right one, so `tsc` is blind to exactly this mistake. Runtime validation at the
fetch boundary would close the class properly; until then, rendering every page against a
live API is the regression net, and a build that passes is not evidence a page works.

## D19 — Errored attempts are excluded from the rates, and coverage is reported

**Spec gap — §6.3.** The scoring rules define pass@1 as "mean attempt pass rate" and
pass^k as "fraction of scenarios where all k attempts passed", but say nothing about an
attempt that never produced a verdict. §8.3 gives `attempts` a `status` and an `error`
column, so the spec clearly anticipates attempts failing for non-model reasons — it just
never says how they score.

Reading §6.3 literally, an attempt with no verdict is not a pass, so it drags pass@1 down.
That is wrong in a way that matters: it reports a provider outage as a capability gap.

**What forced the decision.** The first real k=3 run lost 18 of 30 attempts to a
free-tier rate limit. The summary read `pass@1 0.40` for one model and `0.20` for the
other. Both numbers were meaningless — three of five scenarios never executed — but
nothing in the output said so, and the leaderboard would have ranked two models on them.

**Decision.**

1. `AttemptStatus.ERRORED` is distinct from `FAILED`. Errored means no verdict was
   produced: the provider gave up after its retries, a quota ran out, or the harness
   raised. Failed means the attempt ran and did not pass.
2. **pass@1 is computed over graded attempts only**, and `coverage`
   (graded ÷ total) is reported beside it. Neither number means much without the other.
3. **pass^k is withheld for any scenario that did not grade all k repetitions.** Such a
   scenario leaves *both* sides of the ratio rather than counting as a failure, and
   `scenarios_scored` / `scenarios_total` says how many were eligible. Scoring an
   incomplete scenario as 0 would be a guess; scoring it as a pass would be worse.
4. Any run with coverage below 100% is marked `incomplete`, and that flag is carried
   through the run summary, the leaderboard cell and row, and the compare diff. The CLI
   prints an explicit INCOMPLETE line and exits non-zero.
5. Tokens still count every attempt, because they were genuinely spent. Cost, axis scores
   and latency count only graded attempts.

**Why not just retry harder.** Retries were also fixed (the provider's `Retry-After` is
now honored), but that only reduces how often this happens. Any long run against a real
provider will lose an attempt eventually, and the honest response is to say which numbers
are missing rather than to quietly average over a hole.

## D20 — Model access goes through OpenRouter, with the upstream host pinned

**Spec:** §7 says model access is "any LiteLLM model string" and that keys come from the
environment. It assumes a model string identifies what will answer.

**That assumption breaks on a gateway.** OpenRouter fronts many upstream hosts behind one
slug, and they are not interchangeable: they differ in quantization (an fp8 host and a
bf16 host of the same weights are different models for our purposes), in context window,
and in how faithfully they implement tool calling. Left to its own routing, OpenRouter
picks on price and availability, so two runs of "the same model" can be served by
different hardware — and a re-route looks exactly like a model regression in the
leaderboard.

**Decision.**

1. `.env.example` carries `OPENROUTER_API_KEY` plus an optional `GROQ_API_KEY` for
   calling Groq directly. One key, one bill, one place to rotate.
2. `ModelEntry.provider_routing` pins the upstream: an ordered `order` list with
   `allow_fallbacks: false` and `require_parameters: true`. A pinned host that cannot
   serve the request is an error, never a silent substitution.
3. The routing block travels in `extra_body`, because OpenRouter reads `provider` from the
   request body rather than as an OpenAI-style parameter.
4. **Every `model_response` event records the host that actually served it.** The pin
   states intent; only the trace can prove it held. Where the gateway reports nothing the
   field is `null` rather than a guess.
5. `ModelEntry.effective_params()` builds the request params — base params, reasoning
   effort, routing — in one place, and that same dict is stored on the run. The record and
   the request cannot drift.

**Reasoning effort** is stored per model rather than globally, because the supported
values differ: asking for "medium" uniformly would be silently rounded by some providers
and rejected by others. Each entry names a value that model actually supports, and the run
record shows what was sent.

## D21 — `evalharness run` takes a spend ceiling, default $2.00

**Spec gap.** §6.3 requires cost tracking and §9.3 defines the CLI, but nothing bounds
what a run may spend. `--scenarios all --k 3` against four models is 60 attempts of
unbounded length; a scenario that makes a model loop, or a pricing surprise, spends real
money with no brake.

**Decision.** `--max-cost-usd` (default `2.00`, `0` disables) stops the run as soon as
cumulative cost passes the limit. Attempts that never ran are recorded as **`errored`**,
not failed — so they are excluded from pass@1 and pull coverage down instead of
masquerading as model failures (D19). The stop is reported in the results file and on the
console.

The check is *after* each attempt, not before, because an attempt's cost is not knowable
until it finishes. So the ceiling can be overshot by at most one attempt — stated here
rather than implied, since a hard guarantee would require refusing to start any attempt
that might exceed it, which would make the last portion of every budget unusable.

## D22 — What pre-flight found: three ways provider pinning fails silently

Pinning a provider (D20) turns "OpenRouter picked something odd" into a loud error. The
first real pre-flight turned three of those up, all before a single scenario ran.

**1. `parallel_tool_calls` makes every endpoint ineligible.** No OpenRouter host lists it
in `supported_parameters`, so with `require_parameters: true` it filters out the entire
endpoint set, and `allow_fallbacks: false` turns that into "No endpoints found". The
harness no longer sends it. Nothing is lost: parallel tool calls are the provider default,
and the flag's real use is *disabling* them. Models still batch calls natively, which is
what `meeting-scheduler`'s `parallel` assertion measures.

**2. `temperature` is not a supported parameter on reasoning endpoints.** Both
`gpt-5.6-terra` and `claude-sonnet-5` rejected `temperature: 0` the same way — and did so
with reasoning switched off too, so it is the endpoint, not a conflict between the two.

**Resolution: no model sets temperature. Every one runs at its vendor default.**

The first instinct was to drop temperature only where it was rejected and keep `0` where
it worked — Gemini's endpoint does accept it. That would have been worse than either
extreme. Temperature 0 suppresses run-to-run variance, and pass^k is precisely a measure
of run-to-run variance: pinning half the field to 0 while the other half runs free would
hand the pinned half an advantage on the headline consistency metric, and the leaderboard
would report a sampling decision as a capability difference. A uniform comparison at
vendor defaults is the honest one, even though "everything at temperature 0" sounds more
controlled.

What this costs is worth stating: none of these runs are reproducible call-for-call. That
is the real situation for this model set — two of the four cannot be pinned at all — and
k with pass^k exists to quantify it rather than hide it.

Without `require_parameters` the request would have "succeeded" with the temperature
silently discarded, which is the worse outcome: a run that believes it was deterministic
and was not.

**3. DeepSeek's own endpoint is listed but not routable.** The endpoints API returns a
`deepseek` tag for `deepseek/deepseek-v4-pro-0813`, but pinning it 404s — even with
fallbacks allowed — while the slug routes fine unpinned. The catalogue and the router
disagree.

Substitute: **Fireworks**, pinned. Full 1,048,576-token context, tool calling and
`reasoning: high` both confirmed live. Caveat worth stating plainly: OpenRouter reports its
quantization as `unknown`, not `bf16`. Six hosts were verified routable (Ionstream,
StreamLake, Alibaba, Together, Fireworks, Cloudflare, DigitalOcean); all report `unknown`.
So "non-quantized" here means **not declared quantized** — the fp8 and fp4 hosts were
excluded on their own declarations. It is not positive proof of full precision, and no
endpoint on this slug offers that proof.

**The general lesson.** Each of these would have been invisible without pinning. Case 2 is
the worst of them: it would have produced a complete, plausible run whose stated
configuration was wrong.

## D23 — Model swap, and three scenario fixes the first real run exposed

**DeepSeek out, Gemini in.** `deepseek-v4-pro` produced no data: all 15 attempts hit a
429 from Fireworks' shared upstream pool, and with `allow_fallbacks: false` nothing
substituted. That is the pin behaving correctly — the run reported 0% coverage and
`incomplete` rather than a fabricated 0.00 score (D19) — but it also means the slug is not
dependable on a shared key. Replaced with `gemini-3.5-flash`, pinned to `google-ai-studio`.

Worth noting: unlike the `gpt-5.6-terra` and `claude-sonnet-5` endpoints, Gemini's lists
`temperature` among its supported parameters, so temperature 0 is genuinely honored there
and `reasoning_effort: medium` needs no mapping. Three of four models now run at
temperature 0; the two reasoning endpoints still cannot (D22).

The pin matches three tiers — `google-ai-studio`, `/flex` and `/priority` — and OpenRouter
reports only the provider name, not the tier, so the trace can prove *who* served a call
but not *at which tier*. Flagged rather than papered over, since tier affects latency.

### The scenarios were measuring the wrong thing in two places

**refund-policy: `t2-escalation-offered` is now scenario-scoped (v2).** All three models
failed it, 9/9 — the cross-model check that says "suspect the scenario first". They were
right to. `gpt-5.6-terra` escalated in every run, on turn 1, one turn before the
assertion's window; the failure reason even said `it was called 1x outside this scope`.
Escalating proactively is the preferred behavior done *earlier*, not a miss. Turn-scoping
an assertion whose behavior can legitimately occur in either turn was the bug.

**travel-booking and meeting-scheduler now pre-authorize the action (both v2).** Two of the
three failures examined were models doing the task correctly and then asking permission:

- `claude-sonnet-5` found FL-204, checked all four fare rules, and asked
  *"Shall I go ahead and book this for you?"*
- `gpt-oss-120b` found the one valid slot, Wed 2026-03-04 14:00, and asked
  *"Would you like me to create the event and send the invitations?"*

Both were scored as failures for never calling the tool. But these scenarios exist to test
tool *sequencing* — whether a model checks fare rules before booking, or resolves an
ambiguous name before scheduling — not whether it seeks consent. Penalising a confirmation
prompt measured caution and called it incompetence.

The system prompts now state the action is pre-authorized. `meeting-scheduler` keeps its
clarification requirement intact: ask when ambiguous, act without confirmation once it is
not. That preserves what turn 1 actually tests.

Not every such failure was consent-seeking: `gpt-oss-120b`'s other `meeting-scheduler`
failure concluded no common slot existed when one did. That is a real capability failure
and stays one.

## D24 — 95% Wilson intervals on pass@1, with overlaps flagged

**Spec gap.** §6.3 defines pass@1 and pass^k as point estimates. At k=3 over five
scenarios a model has 15 observations; at k=5, 25. A leaderboard that orders models by a
bare rate at that sample size implies precision it does not have.

**Decision.** Every leaderboard cell and row carries a 95% Wilson score interval, and each
row is flagged `not_significant_vs_leader` when its interval overlaps the top row's.

Wilson rather than the normal approximation because eval results cluster at the
boundaries, where the normal interval misbehaves and can extend outside [0, 1]. Wilson
stays inside, and does not collapse to zero width on a clean sweep: 25/25 gives roughly
[0.87, 1.0] — a perfect small sample still does not prove perfection. At p=0 and p=1 the
bounds are clamped to exactly 0 and 1, which is their analytic value; floating point
otherwise lands a few ulps short and renders 100% as 99.99%.

Overlap is a conservative test: non-overlapping intervals do imply a difference, but
overlapping ones do not prove its absence. The flag therefore says "not significant",
never "the same".

## D25 — Gemini is pinned to the standard tier, confirmed by arithmetic

`google/gemini-3.5-flash` is served by three Google AI Studio tiers — `flex`, standard and
`priority` — that differ in latency and in price. OpenRouter reports only the provider
*name* on a response (`"Google AI Studio"` for all three), and the generation-details
endpoint 404s, so the served tier cannot be read directly.

It can be derived. Per-token pricing differs threefold across the tiers, and `usage.cost`
is returned per call:

| pin | prompt/tok | completion/tok | observed cost | arithmetic |
|---|---|---|---|---|
| `google-ai-studio/flex` | $0.00000075 | $0.0000045 | $0.0003915 | 54×p + 78×c ✓ |
| `google-ai-studio` | $0.0000015 | $0.000009 | **$0.000819** | 54×p + 82×c ✓ |
| `google-ai-studio/priority` | $0.0000027 | $0.0000162 | $0.001053 | 54×p + 56×c ✓ |

The bare `google-ai-studio` pin costs exactly standard-tier rates, so it resolves to the
standard tier rather than the cheapest or the fastest. Confirmed rather than assumed,
because tier affects the latency column that this harness reports.

The general point: a pin is only as good as the evidence that it held. Where the gateway
does not report what served a call, derive it from something it does report.

## D26 — "no data" is not "no run"

The leaderboard skipped any cell whose scenario had zero graded attempts, so a
scenario where *every* attempt errored rendered identically to one that was never
attempted: both said "no run". Those are opposite situations — the first is a failure to
collect data, the second is an absence of it — and the first is the one that needs
attention.

Cells with `attempts > 0 and graded == 0` now render as **"no data"** with the errored
count and a tooltip. Only a genuinely absent cell says "no run".

Found by looking at the dashboard rather than the API: the JSON was correct, and the
omission only became visible once rendered.

## D27 — Per-model RPM limiting, because the cap is per model

**The problem.** A k=5 run lost 48 of 100 attempts to
`new-account-rpm: 20 requests per minute for this model`. The obvious fix — drop provider
concurrency to 1 — is the wrong one. The cap is *per model*, so serialising the whole run
would make four models take four times as long while each still ran at whatever rate a
single attempt happens to produce. A single attempt makes ~10 calls back to back and can
breach 20 rpm on its own, so concurrency was never the lever.

**Decision.** `ModelEntry.rpm` (default 15) and a token bucket per model key, in Redis so
every worker shares one allowance. Each model call takes a token first. Models run fully
in parallel; only the rate per model is bounded.

Continuous refill rather than a fixed window: a window lets a run spend its whole minute's
allowance in one burst and trip the provider anyway, which is the failure this exists to
prevent.

**Two deliberate softnesses.** A limiter that cannot be reached does not block the call —
the request goes out unshaped and the provider's `Retry-After` backoff (D14) remains the
backstop. And `acquire` gives up after 120s rather than waiting forever. In both cases
running slightly too fast beats not running.

The FakeModel entries set `rpm: 0`: scripted transcripts make no request, so there is no
rate to shape. Without that the test suite genuinely slept waiting for tokens it never
needed — it went from 15s to 209s before this was noticed.

## D28 — Latency measures the model, not the queue in front of it

Latency was timed from the start of `complete()`, which included every retry backoff. A
call that succeeded after two 429s reported the whole ordeal as its latency, so the
previous run's p95 of ~20s described rate-limit waiting, not model speed. Adding an RPM
limiter would have made that worse, since queueing for a token would also have counted.

Now `latency_ms` covers **only the successful provider call** — the timer restarts on each
retry — and queue time plus backoff is reported separately as `wait_ms` on the event and
`queue_wait_total_ms` on the run summary. The leaderboard's p50/p95 read `latency_ms`, so
they exclude waits by construction.

Rate-limiter waits are also taken *outside* the turn budget: queueing for a token is the
harness waiting its turn, not the model being slow, and charging it to `turn_timeout_s`
would make the throttle cause spurious timeouts.

## D29 — `git filter-repo` discards uncommitted work

Removing `results/` from history with `git filter-repo --force` also reset the working
tree, silently reverting an uncommitted edit to `config/models.yaml` that had removed
`temperature` from every model.

The k=5 run that followed therefore ran a configuration nobody chose: Gemini and
gpt-oss-120b at `temperature: 0`, the other two at vendor default — precisely the split
D22 argues biases pass^k. The run was already invalid for other reasons, so nothing was
concluded from it, but the near-miss is the point.

What caught it was the run record. Because `effective_params` is stored on the run (D20),
`SELECT model_key, params FROM runs` showed the mismatch directly rather than leaving it
to be inferred from the config file, which by then had been rewritten.

**Practice:** commit before rewriting history, and verify a run's stored params against the
registry rather than assuming the file on disk is what ran.

## D30 — A comparison run refuses to launch from a dirty tree or a drifted registry

**Why both checks exist:** each failure has already happened. A `git filter-repo` reverted
an uncommitted registry edit and the next run went out with temperature pinned on half the
field (D29) — the run recorded a commit that did not describe the code that ran, and sent
params nobody had chosen.

**Decision.** Before any attempt fires, `run` builds a manifest — effective params, pinned
provider, fallbacks, rpm, reasoning effort per model, plus the commit and the scenario
hashes — prints it, and then verifies it:

1. **Dirty tree → refuse.** A run's commit is its provenance; from a dirty tree that
   provenance is false. `--allow-dirty` (CLI) and `allow_dirty` (API) exist for throwaway
   runs, and say plainly in the message that this is the escape hatch.
2. **Registry drift → refuse.** `verify_manifest` deliberately re-reads `models.yaml`
   rather than trusting the manifest it was handed. The point is to catch a file that no
   longer says what the caller believes, not to confirm a manifest agrees with itself.

The API returns 409 rather than 400: the request is well-formed, the repository state
conflicts with it.

Tests pass `allow_dirty` explicitly, because a development tree is routinely dirty and a
test whose outcome depends on that is a flaky test. The guard itself is covered directly.

## D31 — Limiter bypasses are counted, not just logged

The rate limiter deliberately fails open (D27): an unreachable Redis or a 120-second wait
lets the call through unshaped rather than stalling the run. That is the right default and
also a silent one — a run could be almost entirely unthrottled and look identical to a
properly throttled one in every number it reports.

`acquire_detailed` now reports whether it bypassed, each model-response event records it,
and `rate_limit_bypasses` appears on the run summary. A non-zero count means the run was
not fully rate-shaped, which is exactly the context needed to read an unexpected crop of
429s — or to distrust a latency figure.

Log lines were not enough: nobody reads worker logs when the numbers look plausible.

## D32 — The rate limiter was wrong twice, and its tests could not have caught it

The token-bucket limiter (D27) passed every one of its unit tests and still let a run make
28-35 requests per rolling minute against a cap of 20. Two separate defects, both
established from run data rather than reasoned about:

**1. Retries bypassed it entirely.** `acquire` was called once per logical model call, but
the retry loop lives *inside* the provider. One slot therefore covered up to five real HTTP
requests. From the events table:

| model | slots taken | HTTP requests | unshaped |
|---|---|---|---|
| claude-sonnet-5 | 108 | 268 | 160 |
| gemini-3.5-flash | 117 | 340 | 223 |
| gpt-5.6-terra | 93 | 242 | 149 |

The hook now sits on the provider (`RateLimited` protocol) and every attempt in the retry
loop takes a slot.

**2. A token bucket permits about twice its rate in a rolling window.** Capacity equal to
rate means the full burst drains at once and then each refill is consumed as it lands.
Probed directly against Redis at `rpm=15`: 15 granted immediately. A provider counts a
sliding window, so the limiter now does too — a Redis sorted set, with evict-count-add in
one Lua script so two workers cannot both observe the last slot.

**3. A guard band, because scoring and sending are not the same instant.** Even with the
sliding window, measurement showed 13 requests inside the provider's 60 seconds at
`rpm=12`: a slot is scored when granted, the request leaves a moment later, and the two
windows are offset by that gap. Entries are held `WINDOW_SECONDS + 3`, so the limiter
admits marginally *fewer* than `rpm` rather than more. For a guard whose only job is
staying under someone else's cap, erring low is the only safe direction.

**The testing lesson, which is the real one.** The old tests asserted the bucket's internal
arithmetic — tokens in, tokens out — and every one passed while production breached the
cap by 75%. They tested the implementation's story about itself.

The new tests assert what a provider actually measures: **the peak number of requests in
any rolling 60-second window must not exceed rpm**. They run it across OS processes, since
a per-process limiter satisfies a single-process test and fails in a prefork worker. And
`test_a_token_bucket_fails_this_suite` keeps the replaced implementation alive purely to
prove the assertion catches the bug — if that test ever passes, the invariant test has
stopped testing anything.

A test that cannot fail against the bug it was written for is decoration.

## D33 — The turn budget times the model, not the throttle

D32 moved the rate-limiter acquire inside `provider.complete()` so that retries would
take slots too. That was right, and it silently broke something else: the conversation
loop wrapped the whole coroutine in `asyncio.wait_for(timeout=turn_timeout_s)`, so from
that moment queueing for a slot spent the turn's budget.

The damage was measured, not guessed. Across the two models that reached full coverage,
the p95 queue wait was 53–58s against a 120s turn. Fourteen attempts ended on
`turn_timeout_s`, and for thirteen of them the queue wait recorded *within the timed-out
turn alone* was 42–55% of the budget — excluding the final wait, which was never recorded
because the timeout fired inside it. `gpt-oss-120b-groq` scored 0/5 on meeting-scheduler,
four of those five being timeouts; on Groq, attempts that did not time out spent 92–96%
of their wall clock queued. The harness was grading its own throttle.

Two changes:

- `call_timeout_s` is now passed *to* the provider rather than wrapped *around* it, so it
  bounds the HTTP call alone. A timed-out call is not retried — a retry would need a
  budget already established as spent.
- After each successful call the turn deadline is extended by `wait_ms`, which covers
  both limiter queueing and retry backoff. Neither is the model spending its turn.

Because `turn_timeout_s` now deliberately ignores elapsed time, something else has to stop
an attempt that is starved rather than slow. `attempt_timeout_s` (default 1800s) is that
backstop and is the only limit here measured in real wall clock, waits included. It is
deliberately generous: the longest real attempt observed was 380s, and a data-analyst
attempt can legitimately need three turns of 180s model time plus ten queued calls. A
tight cap there would re-create this same bug one level up.

`tests/test_turn_budget.py` pins the guarantee: a limiter wait twice the length of the
whole turn budget, with an instant model, must still pass the turn.
`test_the_old_wrapping_fails_this_suite` reconstructs the previous arrangement and asserts
it dies, so the test cannot quietly stop testing anything.

The general lesson is the one D32 already paid for once: a change that is correct in
isolation can invalidate a guarantee two modules away, and only a test written against the
*guarantee* — not the mechanism — catches it.

## D34 — max_tokens is set explicitly, from this harness's own data

All four models now send `max_tokens: 16384`. The number is not a vendor default or a
round guess: across 1,461 recorded calls the largest single completion any model produced
was 4,184 tokens (gemini-3.5-flash), so 16384 is roughly 3.9x the observed maximum.

Leaving it unset was not the neutral choice it looked like. OpenRouter reserves credit
against the model's own ceiling, so an unset budget reserved 65,536 tokens per call and
two models failed 22 attempts outright with *"This request requires more credits, or fewer
max_tokens. You requested up to 65536 tokens, but can only afford 64772"*. Setting it
explicitly cuts the reservation fourfold and, more importantly, makes the token budget a
recorded run parameter instead of a provider default that can change underneath a
comparison.

Per-call `input_tokens` and `output_tokens` were already stored on `events` (SPEC 8.3) but
not in the payload, which made them easy to miss — an earlier report claimed they were not
recorded at all and sized this value from attempt totals instead. They are now mirrored
into the payload, and `reasoning_tokens` is recorded alongside them where the provider
reports it. It is `None` when unreported rather than `0`: a reasoning model can sit near
its ceiling while its visible content is short, so "not reported" and "did not reason"
size a budget very differently.

## D35 — Truncation is a distinct event, not a silent stop

A response that ends on `finish_reason=length` was cut off by `max_tokens` mid-thought.
Nothing the harness enforces stopped it, so without its own event it grades exactly like a
model that chose to stop: a missing tool call reads as a decision rather than an
interruption.

`EventType.TRUNCATED` records it with the completion and reasoning token counts, the
attempt carries a `truncated` flag, and the run summary carries `truncated_attempts`, which
the dashboard surfaces as a banner and the trace viewer as its own card.

Such an attempt is still graded and still counted in the rates — it did produce a verdict,
and dropping it would quietly shrink coverage. It is flagged, not excluded.

For the record: this occurred zero times in the run that motivated it. All 291 calls
finished on `tool_calls` or `stop`. The blank response that prompted the investigation was
a turn timeout (D33), not truncation. The guard is here for the next run, and the fact that
it would have fired zero times is itself worth knowing.

## D36 — A run refuses to launch that the account cannot pay for

Credits running out mid-run does not fail cleanly. It fails *partially*: some attempts
complete, others error, and the resulting coverage gap looks exactly like a capability
difference until someone reads the error text. That is how 22 attempts were lost across
claude-sonnet-5 and gemini-3.5-flash, and the scores that run produced were not comparable
to the two models that finished.

`evalharness run` now queries OpenRouter's credits endpoint before launching and refuses
if the balance is below the worst case the spend ceiling allows (`max_cost_usd` x models).
Both numbers are printed either way, so a run that proceeds has shown its work too.

The check is deliberately conservative: it compares against what the run is *permitted* to
spend, not what it is expected to spend, because "expected" is precisely the estimate that
was wrong when the credits ran out. A run with no ceiling is refused rather than waved
through — an unbounded run cannot be shown to be affordable. `--skip-balance-check` exists
for offline and non-OpenRouter work.

## D37 — data-analyst separates touching the decoy from being fooled by it

`orders_archive` is a pre-2024 decoy table. The graded assertion, `t1-monthly-revenue`,
compares the returned rows against the expected 2025 figures, so it only fails when the
decoy actually corrupted the answer — which is the right thing to grade, but it means a
run could not report how often the decoy was *touched*.

Those are different behaviours. Querying the archive while exploring is reasonable; basing
the answer on it is not. `t1-archive-not-queried` and `t2-archive-not-queried` are soft
`args_not_contains` assertions that record the first, leaving the required assertions to
grade the second. A run now reports both counts, and a soft failure moves the restraint
axis without failing the turn.

Scenario version bumped to 2.
