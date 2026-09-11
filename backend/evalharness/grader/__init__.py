"""Assertion evaluation and scoring (SPEC 4.3, 6.2, 6.3).

The grader is pure: it reads the records an attempt produced and returns
verdicts. It never calls a model (except the opt-in judge, which lives behind
its own flag) and never touches the clock.
"""

from __future__ import annotations

from evalharness.grader.assertions import GradingContext, evaluate
from evalharness.grader.result_match import ResultMatchOutcome, match_result
from evalharness.grader.scoring import (
    RunSummary,
    attempt_passed,
    critical_failures,
    required_failures,
    score_axes,
    summarize_run,
    total_cost,
)
from evalharness.loader.scenario_loader import LoadedScenario
from evalharness.schema.assertions import JudgeAssertion
from evalharness.schema.enums import Scope
from evalharness.schema.runtime import (
    AssertionResult,
    AttemptResult,
    ToolCallRecord,
    TurnRecord,
)

__all__ = [
    "GradingContext",
    "ResultMatchOutcome",
    "RunSummary",
    "attempt_passed",
    "critical_failures",
    "evaluate",
    "grade_scenario_scope",
    "grade_turn",
    "match_result",
    "required_failures",
    "score_axes",
    "summarize_run",
    "total_cost",
]


def grade_turn(
    loaded: LoadedScenario, turn: TurnRecord, all_records: list[ToolCallRecord]
) -> list[AssertionResult]:
    """Grade the ``scope: turn`` assertions declared on one turn (SPEC 6.1 step 5).

    The assertions see only this turn's tool calls. ``all_records`` is the whole
    attempt so far, used purely to sharpen failure messages -- "it was called in
    turn 0" is far more useful in a trace than "it was never called".
    """
    config_turn = loaded.scenario.turns[turn.index]
    out_of_scope = [r for r in all_records if r.turn_index != turn.index]
    results: list[AssertionResult] = []
    for assertion in config_turn.assertions:
        if assertion.scope is not Scope.TURN or isinstance(assertion, JudgeAssertion):
            continue
        context = GradingContext(
            loaded=loaded,
            records=turn.tool_calls,
            final_response=turn.final_response,
            turn_index=turn.index,
            out_of_scope=out_of_scope,
        )
        results.append(evaluate(assertion, context))
    return results


def grade_scenario_scope(loaded: LoadedScenario, result: AttemptResult) -> list[AssertionResult]:
    """Grade every ``scope: scenario`` assertion over the whole attempt (SPEC 6.1 step 7)."""
    all_records = [record for turn in result.turns for record in turn.tool_calls]
    final_response = next(
        (turn.final_response for turn in reversed(result.turns) if turn.final_response is not None),
        None,
    )
    results: list[AssertionResult] = []
    for config_turn in loaded.scenario.turns:
        for assertion in config_turn.assertions:
            if assertion.scope is not Scope.SCENARIO or isinstance(assertion, JudgeAssertion):
                continue
            context = GradingContext(
                loaded=loaded,
                records=all_records,
                final_response=final_response,
                turn_index=None,
                out_of_scope=(),
            )
            results.append(evaluate(assertion, context))
    return results
