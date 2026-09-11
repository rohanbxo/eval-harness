"""Runtime domain models: the objects produced while an attempt executes.

These are the shared vocabulary between the engine, the runner and the grader.
Nothing here touches the network or the wall clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from evalharness.schema.enums import Axis, EventType, Severity


class ToolCall(BaseModel):
    """One tool call requested by the model."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    raw_arguments: str | None = Field(
        default=None, description="Original JSON string, kept when it failed to parse."
    )
    parse_error: str | None = None


class ToolResult(BaseModel):
    """The engine's answer to a tool call (SPEC 5.1)."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    content: JsonValue = None
    error: str | None = None

    @classmethod
    def failure(cls, error: str, content: JsonValue = None) -> ToolResult:
        if content is None:
            content = {"error": error}
        return cls(ok=False, error=error, content=content)


class AssistantMessage(BaseModel):
    """A normalized model response."""

    model_config = ConfigDict(extra="forbid")

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    raw: dict[str, Any] | None = None


class Event(BaseModel):
    """One ordered entry in an attempt's audit trail (SPEC 8.3)."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    turn_index: int | None
    type: EventType
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    created_at: datetime | None = None


class ToolCallRecord(BaseModel):
    """A call/result pair, the unit the grader reasons over."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    turn_index: int
    step: int = Field(description="Which model response within the turn produced this call.")
    batch_index: int = Field(description="Position within a parallel batch of calls.")
    call: ToolCall
    result: ToolResult
    faulted: bool = False

    @property
    def name(self) -> str:
        return self.call.name

    @property
    def arguments(self) -> dict[str, JsonValue]:
        return self.call.arguments


class TurnRecord(BaseModel):
    """Everything that happened during one scripted user turn."""

    model_config = ConfigDict(extra="forbid")

    index: int
    user_message: str
    final_response: str | None = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    steps: int = 0
    limit_exceeded: bool = False
    limit_reason: str | None = None
    passed: bool = True


class AssertionResult(BaseModel):
    """The graded outcome of one assertion (SPEC 8.3)."""

    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    type: str
    axis: Axis
    severity: Severity
    turn_index: int | None
    passed: bool
    reason: str = ""
    details: dict[str, JsonValue] = Field(default_factory=dict)
    non_deterministic: bool = False


class AttemptResult(BaseModel):
    """The full outcome of one scenario x repetition (SPEC 6.3)."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    repetition: int
    passed: bool = False
    critical_failure: bool = False
    axis_scores: dict[str, float] = Field(default_factory=dict)
    turns: list[TurnRecord] = Field(default_factory=list)
    assertion_results: list[AssertionResult] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    error: str | None = None
    errored: bool = Field(
        default=False,
        description=(
            "The attempt never produced a verdict: the provider failed after its "
            "retries, a quota ran out, or the harness raised. Such an attempt is "
            "missing data, not evidence about the model, so scoring excludes it."
        ),
    )

    @property
    def graded(self) -> bool:
        """Whether this attempt yielded a verdict that belongs in the scores."""
        return not self.errored
