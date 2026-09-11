"""Assertion configuration models (SPEC 4.3).

Assertions are a discriminated union on ``type``. Every variant carries the
common ``id``/``axis``/``severity``/``scope`` header; the rest of the fields are
specific to the check being made.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from evalharness.schema.enums import Axis, ResultMatchMode, Scope, Severity
from evalharness.schema.matchers import ArgMatchers, CallSelector, CountConstraint


class _AssertionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, description="Unique within the scenario.")
    axis: Axis
    severity: Severity = Severity.REQUIRED
    scope: Scope = Scope.TURN
    description: str | None = None


class ToolCalledAssertion(_AssertionBase):
    """At least one call to ``tool`` whose args satisfy the matchers."""

    type: Literal["tool_called"] = "tool_called"
    tool: str
    args: ArgMatchers = Field(default_factory=dict)
    count: CountConstraint | None = None


class ToolNotCalledAssertion(_AssertionBase):
    """No call to ``tool`` (optionally only calls matching ``args``)."""

    type: Literal["tool_not_called"] = "tool_not_called"
    tool: str
    args: ArgMatchers = Field(default_factory=dict)


class OrderAssertion(_AssertionBase):
    """A call matching ``before`` happened before any call matching ``after``."""

    type: Literal["order"] = "order"
    before: CallSelector
    after: CallSelector


class ParallelAssertion(_AssertionBase):
    """The listed calls all appear in the same assistant message."""

    type: Literal["parallel"] = "parallel"
    calls: list[CallSelector] = Field(min_length=2)


class NoToolCallsAssertion(_AssertionBase):
    """The model made zero tool calls this turn."""

    type: Literal["no_tool_calls"] = "no_tool_calls"


class ClarificationAssertion(_AssertionBase):
    """No blocked tools were called and the final message asks a question."""

    type: Literal["clarification"] = "clarification"
    blocked_tools: list[str] = Field(default_factory=list)
    question_pattern: str = Field(
        default=r"\?",
        description="Heuristic for 'asks the user something'. Default: contains a question mark.",
    )


class RecoveredAssertion(_AssertionBase):
    """A call to ``tool`` errored and a later call to the same tool succeeded."""

    type: Literal["recovered"] = "recovered"
    tool: str


class ToolResultMatchesAssertion(_AssertionBase):
    """Some successful call to ``tool`` produced a result matching ``expected``."""

    type: Literal["tool_result_matches"] = "tool_result_matches"
    tool: str
    expected: str = Field(description="Path to the expected-result JSON, relative to scenario dir.")
    mode: ResultMatchMode = ResultMatchMode.VALUE_MULTISET
    tolerance: float = Field(default=0.01, ge=0)


class ArgsNotContainsAssertion(_AssertionBase):
    """No call to ``tool`` contains ``pattern`` in any string argument (deep)."""

    type: Literal["args_not_contains"] = "args_not_contains"
    tool: str
    pattern: str
    ci: bool = False


class ResponseMatchesAssertion(_AssertionBase):
    """Regex against the final assistant message of the turn."""

    type: Literal["response_matches"] = "response_matches"
    pattern: str
    ci: bool = False


class ResponseNotMatchesAssertion(_AssertionBase):
    """Inverse of ``response_matches``."""

    type: Literal["response_not_matches"] = "response_not_matches"
    pattern: str
    ci: bool = False


class JudgeAssertion(_AssertionBase):
    """LLM-judge rubric. Disabled by default; always flagged non-deterministic."""

    type: Literal["judge"] = "judge"
    rubric: str
    judge_model_key: str | None = None


Assertion = Annotated[
    ToolCalledAssertion
    | ToolNotCalledAssertion
    | OrderAssertion
    | ParallelAssertion
    | NoToolCallsAssertion
    | ClarificationAssertion
    | RecoveredAssertion
    | ToolResultMatchesAssertion
    | ArgsNotContainsAssertion
    | ResponseMatchesAssertion
    | ResponseNotMatchesAssertion
    | JudgeAssertion,
    Field(discriminator="type"),
]
