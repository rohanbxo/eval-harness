"""API request and response models (SPEC 9.2).

The Next.js app generates its TypeScript from this OpenAPI schema, so every
response is fully typed: no bare ``dict``/``Any`` leaks into a response model.
``JsonValue`` is used where the value genuinely is arbitrary JSON (event
payloads, model params) -- it is a real recursive JSON type, not an escape hatch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from evalharness.schema.enums import AttemptStatus, Axis, EventType, RunStatus, Severity
from evalharness.schema.registry import Pricing
from evalharness.schema.scenario import Scenario
from evalharness.schema.tools import FixtureFile, ToolDefinition


class ApiModel(BaseModel):
    """Base for every API model: strict, and stable in the generated schema."""

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------- health


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"
    version: str
    database: Literal["ok", "unavailable"] = "ok"


# ---------------------------------------------------------------------- scenarios


class ScenarioSummary(ApiModel):
    """One row of ``GET /api/scenarios``; ``valid`` reflects load-time validation."""

    id: str
    title: str = ""
    description: str = ""
    version: int = 0
    axes: list[Axis] = Field(default_factory=list)
    turn_count: int = 0
    tool_count: int = 0
    assertion_count: int = 0
    config_hash: str = ""
    valid: bool = True
    error: str | None = None


class ScenarioListResponse(ApiModel):
    scenarios: list[ScenarioSummary]


class ScenarioDetail(ApiModel):
    """Full definition, exactly as the harness loaded it."""

    id: str
    config_hash: str
    valid: bool = True
    error: str | None = None
    scenario: Scenario | None = None
    tools: list[ToolDefinition] = Field(default_factory=list)
    fixtures: dict[str, FixtureFile] = Field(default_factory=dict)
    expected: dict[str, JsonValue] = Field(default_factory=dict)
    transcripts: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------------- models


class ModelInfo(ApiModel):
    """A registry entry plus key availability. The key itself is never returned."""

    key: str
    display_name: str
    litellm_model: str
    provider: str
    params: dict[str, JsonValue] = Field(default_factory=dict)
    supports_parallel_tool_calls: bool = True
    supports_tool_calling: bool = True
    api_key_env: str | None = None
    api_key_present: bool = Field(
        description="Whether api_key_env is set in the environment. Never the value."
    )
    pricing_override: Pricing | None = None


class ModelListResponse(ApiModel):
    models: list[ModelInfo]


# --------------------------------------------------------------------------- runs


class RunCreate(ApiModel):
    """Body of ``POST /api/runs`` (SPEC 9.2)."""

    model_key: str
    scenario_ids: list[str] = Field(min_length=1)
    k: int = Field(default=1, ge=1, le=50)
    params_override: dict[str, JsonValue] | None = None
    transcript: str | None = Field(
        default=None,
        description="FakeModel only: transcript stem to replay (default 'golden').",
    )
    max_cost_usd: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Stop the run once its cumulative cost passes this. Remaining attempts are "
            "recorded as errored, so they lower coverage rather than counting as model "
            "failures. Null disables the guard (DECISIONS D21)."
        ),
    )


class ScenarioStats(ApiModel):
    """Per-scenario slice of a run summary (SPEC 6.3)."""

    scenario_id: str
    config_hash: str = ""
    attempts: int = 0
    completed: int = 0
    errored: int = 0
    coverage: float = Field(default=0.0, description="Graded attempts / attempts.")
    complete: bool = Field(
        default=True,
        description="Every repetition was graded, so pass^k is meaningful here.",
    )
    passed: int = 0
    pass_at_1: float = 0.0
    pass_hat_k: float = Field(
        default=0.0, description="Only meaningful when `complete`; 0.0 otherwise."
    )
    axis_scores: dict[str, float] = Field(default_factory=dict)
    cost_usd: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    mean_steps_per_turn: float | None = None


class RunSummary(ApiModel):
    """The ``runs.summary`` jsonb blob, computed by ``finalize_run`` (SPEC 6.3)."""

    k: int = 1
    total_attempts: int = 0
    completed_attempts: int = 0
    coverage: float = Field(
        default=0.0,
        description="Graded attempts / total attempts. Rates below cover only those.",
    )
    incomplete: bool = Field(
        default=False,
        description="Some attempt never produced a verdict, so the rates are partial.",
    )
    scenarios_scored: int = Field(
        default=0, description="Scenarios whose every repetition was graded (pass^k basis)."
    )
    scenarios_total: int = 0
    passed_attempts: int = 0
    failed_attempts: int = 0
    cancelled_attempts: int = 0
    errored_attempts: int = 0
    pass_at_1: float = 0.0
    pass_hat_k: float = 0.0
    axis_scores: dict[str, float] = Field(default_factory=dict)
    cost_usd: float | None = Field(
        default=None, description="Null when any attempt's cost is unknown; never guessed."
    )
    latency_p50_ms: float | None = Field(
        default=None,
        description="Successful model-call duration only; excludes throttle and backoff.",
    )
    latency_p95_ms: float | None = None
    queue_wait_total_ms: int = Field(
        default=0,
        description="Total time spent rate-limited or backing off across the run.",
    )
    queue_wait_p95_ms: float | None = None
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    mean_steps_per_turn: float | None = None
    per_scenario: list[ScenarioStats] = Field(default_factory=list)


class AttemptSummary(ApiModel):
    id: str
    run_id: str
    scenario_id: str
    repetition: int
    status: AttemptStatus
    passed: bool | None = None
    critical_failure: bool = False
    axis_scores: dict[str, float] = Field(default_factory=dict)
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None


class Run(ApiModel):
    """A run without its (large) scenario snapshots."""

    id: str
    created_at: datetime
    status: RunStatus
    model_key: str
    litellm_model: str
    params: dict[str, JsonValue] = Field(default_factory=dict)
    k: int
    scenario_ids: list[str] = Field(default_factory=list)
    config_hashes: dict[str, str] = Field(default_factory=dict)
    git_commit: str = "unknown"
    harness_version: str = "0.0.0"
    summary: RunSummary | None = None
    attempt_counts: dict[str, int] = Field(
        default_factory=dict, description="Attempt count by status."
    )


class RunDetail(Run):
    attempts: list[AttemptSummary] = Field(default_factory=list)


class RunListResponse(ApiModel):
    items: list[Run]
    total: int
    limit: int
    offset: int


# -------------------------------------------------------------------------- trace


class EventOut(ApiModel):
    id: int
    attempt_id: str
    turn_index: int | None = None
    seq: int
    type: EventType
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    created_at: datetime


class TurnOut(ApiModel):
    id: int
    attempt_id: str
    index: int
    user_message: str
    final_response: str | None = None
    passed: bool = True
    limit_exceeded: bool = False


class AssertionResultOut(ApiModel):
    id: int
    attempt_id: str
    turn_index: int | None = None
    assertion_id: str
    type: str
    axis: Axis
    severity: Severity
    passed: bool
    reason: str = ""
    details: dict[str, JsonValue] = Field(default_factory=dict)
    non_deterministic: bool = False


class AttemptTrace(ApiModel):
    """Everything the trace viewer needs for one attempt (SPEC 10.1)."""

    attempt: AttemptSummary
    run: Run
    scenario_id: str
    scenario_title: str = ""
    config_hash: str = ""
    turns: list[TurnOut] = Field(default_factory=list)
    events: list[EventOut] = Field(default_factory=list)
    assertion_results: list[AssertionResultOut] = Field(default_factory=list)


# -------------------------------------------------------------------- leaderboard


class LeaderboardCell(ApiModel):
    """One model x scenario cell, from a single run (SPEC 9.2)."""

    model_key: str
    scenario_id: str
    run_id: str
    created_at: datetime
    k: int
    config_hash: str = ""
    config_changed: bool = Field(
        default=False,
        description="True when this cell's config_hash differs from the scenario on disk.",
    )
    attempts: int = 0
    incomplete: bool = Field(
        default=False,
        description="Some repetition never produced a verdict; treat the rates as partial.",
    )
    coverage: float = 1.0
    pass_at_1: float = 0.0
    passed: int = 0
    graded: int = 0
    pass_at_1_low: float = Field(default=0.0, description="95% Wilson lower bound.")
    pass_at_1_high: float = Field(default=1.0, description="95% Wilson upper bound.")
    pass_hat_k: float = 0.0
    axis_scores: dict[str, float] = Field(default_factory=dict)
    cost_usd: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None


class LeaderboardRow(ApiModel):
    model_key: str
    display_name: str
    litellm_model: str = ""
    scenarios_covered: int = 0
    pass_at_1: float = 0.0
    passed: int = 0
    graded: int = 0
    pass_at_1_low: float = Field(default=0.0, description="95% Wilson lower bound.")
    pass_at_1_high: float = Field(default=1.0, description="95% Wilson upper bound.")
    not_significant_vs_leader: bool = Field(
        default=False,
        description=(
            "This row's 95% interval overlaps the top row's, so the gap between them "
            "is not established at that level. True for the leader itself."
        ),
    )
    pass_hat_k: float = 0.0
    axis_scores: dict[str, float] = Field(default_factory=dict)
    cost_usd: float | None = None
    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    has_config_drift: bool = False
    has_incomplete: bool = Field(
        default=False, description="At least one cell is missing attempts."
    )
    cells: list[LeaderboardCell] = Field(default_factory=list)


class LeaderboardScenario(ApiModel):
    scenario_id: str
    title: str = ""
    current_config_hash: str | None = Field(
        default=None, description="Hash of the scenario on disk right now, if it still exists."
    )


class LeaderboardResponse(ApiModel):
    generated_at: datetime
    scenarios: list[LeaderboardScenario] = Field(default_factory=list)
    rows: list[LeaderboardRow] = Field(default_factory=list)


# ------------------------------------------------------------------------ compare


class ScenarioDiff(ApiModel):
    scenario_id: str
    config_hash_a: str = ""
    config_hash_b: str = ""
    config_changed: bool = False
    incomplete_a: bool = False
    incomplete_b: bool = False
    pass_at_1_a: float | None = None
    pass_at_1_b: float | None = None
    pass_at_1_delta: float | None = None
    pass_hat_k_a: float | None = None
    pass_hat_k_b: float | None = None
    pass_hat_k_delta: float | None = None
    axis_scores_a: dict[str, float] = Field(default_factory=dict)
    axis_scores_b: dict[str, float] = Field(default_factory=dict)
    axis_deltas: dict[str, float] = Field(default_factory=dict)
    cost_usd_a: float | None = None
    cost_usd_b: float | None = None
    cost_usd_delta: float | None = None
    latency_p50_ms_a: float | None = None
    latency_p50_ms_b: float | None = None
    latency_p50_ms_delta: float | None = None


class AssertionFlip(ApiModel):
    """An assertion whose outcome moved between the two runs."""

    scenario_id: str
    assertion_id: str
    type: str = ""
    axis: Axis | None = None
    severity: Severity | None = None
    pass_rate_a: float | None = None
    pass_rate_b: float | None = None
    direction: Literal["fixed", "broken", "improved", "regressed", "added", "removed"]


class CompareResponse(ApiModel):
    run_a: Run
    run_b: Run
    scenarios: list[ScenarioDiff] = Field(default_factory=list)
    flipped_assertions: list[AssertionFlip] = Field(default_factory=list)


# ---------------------------------------------------------------------------- SSE


#: The ``event:`` name of a Server-Sent Event on ``GET /api/runs/{id}/stream``.
ProgressEventType = Literal[
    "snapshot",
    "run_started",
    "attempt_started",
    "attempt_finished",
    "run_completed",
    "heartbeat",
]


class ProgressEvent(ApiModel):
    """One Server-Sent Event on ``GET /api/runs/{id}/stream`` (SPEC 9.1).

    Emitted as ``event: <type>`` with this object as the JSON ``data`` payload.
    """

    type: ProgressEventType
    run_id: str
    run_status: RunStatus | None = None
    attempt_id: str | None = None
    scenario_id: str | None = None
    repetition: int | None = None
    attempt_status: AttemptStatus | None = None
    passed: bool | None = None
    completed_attempts: int | None = None
    total_attempts: int | None = None
    attempt_counts: dict[str, int] = Field(default_factory=dict)
    message: str | None = None
    ts: datetime


class CancelResponse(ApiModel):
    run: RunDetail
    cancelled_attempts: int = 0
    already_terminal: bool = False
