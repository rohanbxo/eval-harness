"""Matcher configuration models (SPEC 4.4).

A matcher is a declarative predicate over a single JSON value. An ``args`` block
maps an argument name to a matcher; unlisted arguments are ignored.

These are *config* models only -- the evaluation logic lives in
``evalharness.engine.matching`` and must stay pure and deterministic.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

#: Matcher keys that assert something about the value, as opposed to modifiers
#: like ``ci`` that only tune how a comparison is performed.
PRIMARY_MATCHER_KEYS: frozenset[str] = frozenset(
    {
        "equals",
        "any_of",
        "regex",
        "contains",
        "not_contains",
        "gte",
        "lte",
        "date_equals",
        "datetime_equals",
        "includes_all",
        "exists",
        "absent",
    }
)


class Matcher(BaseModel):
    """A predicate over one JSON value.

    Exactly one primary key must be set. ``ci`` and ``partial`` are modifiers
    that refine string comparisons rather than assertions in their own right.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    equals: JsonValue = None
    any_of: list[JsonValue] | None = None
    regex: str | None = None
    contains: JsonValue = None
    not_contains: JsonValue = None
    gte: float | None = None
    lte: float | None = None
    date_equals: str | None = None
    datetime_equals: str | None = None
    includes_all: list[Matcher] | None = None
    exists: bool | None = None
    absent: bool | None = None

    # Modifiers
    ci: bool = False
    partial: bool = False

    @property
    def primary_keys(self) -> set[str]:
        """The primary matcher keys explicitly supplied in config."""
        return {k for k in self.model_fields_set if k in PRIMARY_MATCHER_KEYS}

    @model_validator(mode="after")
    def _exactly_one_primary(self) -> Matcher:
        supplied = self.primary_keys
        # gte/lte may legitimately be combined to express a numeric range.
        if supplied == {"gte", "lte"}:
            return self
        if len(supplied) != 1:
            listed = ", ".join(sorted(supplied)) or "none"
            raise ValueError(
                f"a matcher needs exactly one of [{', '.join(sorted(PRIMARY_MATCHER_KEYS))}] "
                f"(or gte+lte together); got: {listed}"
            )
        return self


#: An ``args`` block: argument name -> matcher. Unlisted arguments are ignored.
#: Declare defaults at the use site (``Field(default_factory=dict)``) so both
#: pydantic and mypy see them.
ArgMatchers = dict[str, Matcher]


class CountConstraint(BaseModel):
    """Constrains how many calls matched (SPEC 4.3, ``tool_called``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exactly: int | None = Field(default=None, ge=0)
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _at_least_one(self) -> CountConstraint:
        if self.exactly is None and self.min is None and self.max is None:
            raise ValueError("count needs at least one of: exactly, min, max")
        if self.exactly is not None and (self.min is not None or self.max is not None):
            raise ValueError("count.exactly cannot be combined with min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"count.min ({self.min}) exceeds count.max ({self.max})")
        return self


class CallSelector(BaseModel):
    """Selects tool calls by name and (optionally) argument matchers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool: str
    args: ArgMatchers = Field(default_factory=dict)
