"""Scenario configuration models (SPEC 4.1, 5.4)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from evalharness.schema.assertions import Assertion
from evalharness.schema.enums import Axis


class Limits(BaseModel):
    """Stopping conditions for a turn, and a backstop for the whole attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_steps_per_turn: int = Field(default=12, ge=1, le=100)
    turn_timeout_s: float = Field(
        default=120.0,
        gt=0,
        description=(
            "Budget for the model's own work in one turn. Time the harness spends "
            "queueing for a rate-limit slot or backing off after a 429 does not "
            "count against it -- that would grade the throttle, not the model "
            "(DECISIONS D33)."
        ),
    )
    attempt_timeout_s: float = Field(
        default=600.0,
        gt=0,
        description=(
            "Hard wall-clock ceiling for one attempt, waits included. Because "
            "turn_timeout_s deliberately ignores queueing, something has to stop "
            "an attempt that is starved rather than slow; this is that backstop, "
            "and it is the only limit here measured in real elapsed time."
        ),
    )


class Fault(BaseModel):
    """A scripted tool failure injected on a specific call (SPEC 5.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    on_call: int = Field(ge=1, description="1-indexed call count for this tool within the attempt.")
    response: JsonValue
    ok: bool = Field(default=False, description="Fault responses count as errors by default.")


class Turn(BaseModel):
    """One scripted user message and the assertions graded after it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user: str = Field(min_length=1)
    assertions: list[Assertion] = Field(default_factory=list)


class Scenario(BaseModel):
    """A full scenario definition, as parsed from ``scenario.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$", description="Must equal the directory name.")
    version: int = Field(ge=1, description="Bump on any change that affects grading.")
    title: str
    description: str = ""
    axes: list[Axis] = Field(default_factory=list)
    clock: datetime = Field(description="Frozen 'now'; injected into the system prompt.")
    system_prompt: str
    limits: Limits = Field(default_factory=Limits)
    tools: str = Field(default="tools.json", description="Tool file, relative to scenario dir.")
    fixtures: str = Field(default="fixtures/", description="Fixture dir, relative to scenario dir.")
    faults: list[Fault] = Field(default_factory=list)
    continue_on_fail: bool = True
    turns: list[Turn] = Field(min_length=1)

    @model_validator(mode="after")
    def _clock_needs_offset(self) -> Scenario:
        if self.clock.tzinfo is None:
            raise ValueError("clock must include a UTC offset, e.g. 2026-03-02T09:00:00+04:00")
        return self

    @model_validator(mode="after")
    def _assertion_ids_unique(self) -> Scenario:
        seen: dict[str, int] = {}
        for turn_index, turn in enumerate(self.turns):
            for assertion in turn.assertions:
                if assertion.id in seen:
                    raise ValueError(
                        f"duplicate assertion id {assertion.id!r}: "
                        f"turn {seen[assertion.id]} and turn {turn_index}"
                    )
                seen[assertion.id] = turn_index
        return self
