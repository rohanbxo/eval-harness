"""Fixed enumerations shared across config and results (SPEC 4.3, 8.3)."""

from __future__ import annotations

from enum import StrEnum


class Axis(StrEnum):
    """Capability dimension an assertion measures. Fixed set -- SPEC 4.3."""

    SELECTION = "selection"
    ARGUMENTS = "arguments"
    ORDERING = "ordering"
    RESTRAINT = "restraint"
    RECOVERY = "recovery"
    STATE = "state"
    CLARIFICATION = "clarification"
    SAFETY = "safety"


class Severity(StrEnum):
    """How much an assertion failure costs.

    ``critical`` aborts the attempt (everything is still recorded),
    ``required`` fails the turn, ``soft`` only moves axis scores.
    """

    CRITICAL = "critical"
    REQUIRED = "required"
    SOFT = "soft"


class Scope(StrEnum):
    """Which events an assertion sees."""

    TURN = "turn"
    SCENARIO = "scenario"


class AssertionType(StrEnum):
    TOOL_CALLED = "tool_called"
    TOOL_NOT_CALLED = "tool_not_called"
    ORDER = "order"
    PARALLEL = "parallel"
    NO_TOOL_CALLS = "no_tool_calls"
    CLARIFICATION = "clarification"
    RECOVERED = "recovered"
    TOOL_RESULT_MATCHES = "tool_result_matches"
    ARGS_NOT_CONTAINS = "args_not_contains"
    RESPONSE_MATCHES = "response_matches"
    RESPONSE_NOT_MATCHES = "response_not_matches"
    JUDGE = "judge"


class ResultMatchMode(StrEnum):
    """Comparison strategy for ``tool_result_matches`` (SPEC 6.2)."""

    VALUE_MULTISET = "value_multiset"
    ROWS_EXACT = "rows_exact"


class EventType(StrEnum):
    """Ordered audit-trail event kinds (SPEC 8.3)."""

    SYSTEM = "system"
    USER_MESSAGE = "user_message"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FAULT = "fault"
    RETRY = "retry"
    LIMIT_EXCEEDED = "limit_exceeded"
    TRUNCATED = "truncated"
    ERROR = "error"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    """Where an attempt ended up.

    ``COMPLETED`` means the attempt ran to the end and was graded -- whether or
    not it passed. ``ERRORED`` means it never produced a verdict at all, because
    the provider gave up, a quota ran out, or the harness itself raised. The
    distinction matters for scoring: an errored attempt is missing data, and
    counting it as a failure would blame the model for an outage (SPEC 6.3 does
    not say which to do; see docs/DECISIONS.md D19).
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ERRORED = "errored"
    CANCELLED = "cancelled"

    @property
    def is_graded(self) -> bool:
        """True when this attempt produced a pass/fail verdict worth scoring."""
        return self in {AttemptStatus.COMPLETED, AttemptStatus.FAILED}


class MockKind(StrEnum):
    FIXTURE = "fixture"
    HANDLER = "handler"
