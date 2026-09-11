"""FakeModel transcript schema (SPEC 6.4).

A transcript scripts the assistant side of a conversation so scenarios can be
tested without touching a provider. Every scenario ships one ``golden.yaml``
plus at least one ``fail_*.yaml``; together they are the scenario's test suite.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class ScriptedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)


class ScriptedStep(BaseModel):
    """One scripted assistant message.

    Either it requests tool calls (and the runner loops again) or it returns
    plain content (which ends the turn).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str | None = None
    tool_calls: list[ScriptedToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_something(self) -> ScriptedStep:
        if self.content is None and not self.tool_calls:
            raise ValueError("a scripted step needs content, tool_calls, or both")
        return self


class ScriptedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: list[ScriptedStep] = Field(min_length=1)


class Transcript(BaseModel):
    """A scripted run plus the verdict the grader must produce for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str
    description: str = ""
    expect_pass: bool = Field(description="Whether the grader must mark the attempt as passed.")
    expect_failures: list[str] = Field(
        default_factory=list,
        description="Assertion ids that must fail. Must be exact when expect_pass is false.",
    )
    expect_all_axes_full: bool = Field(
        default=False,
        description="Golden transcripts set this: every axis score must be 1.0.",
    )
    turns: list[ScriptedTurn] = Field(min_length=1)

    @model_validator(mode="after")
    def _coherent_expectations(self) -> Transcript:
        if self.expect_pass and self.expect_failures:
            raise ValueError("expect_pass is true but expect_failures is non-empty")
        if not self.expect_pass and not self.expect_failures:
            raise ValueError("a failing transcript must name the assertion ids it fails on")
        return self
