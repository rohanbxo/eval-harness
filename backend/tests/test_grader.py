"""Assertion evaluation (SPEC 4.3, 6.2).

Every assertion type is exercised in both directions, and the failure *reasons*
are asserted too: a reason that does not name the offending call or the matcher
that rejected it is useless in the trace viewer.
"""

from __future__ import annotations

import dataclasses
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from evalharness.grader import GradingContext, evaluate, grade_scenario_scope, grade_turn
from evalharness.grader.result_match import extract_numbers, extract_rows, match_result
from evalharness.loader import LoadedScenario, load_scenario
from evalharness.schema.assertions import (
    ArgsNotContainsAssertion,
    Assertion,
    ClarificationAssertion,
    JudgeAssertion,
    NoToolCallsAssertion,
    OrderAssertion,
    ParallelAssertion,
    RecoveredAssertion,
    ResponseMatchesAssertion,
    ResponseNotMatchesAssertion,
    ToolCalledAssertion,
    ToolNotCalledAssertion,
    ToolResultMatchesAssertion,
)
from evalharness.schema.enums import Axis, ResultMatchMode, Scope, Severity
from evalharness.schema.matchers import CallSelector, CountConstraint, Matcher
from evalharness.schema.runtime import (
    AttemptResult,
    ToolCall,
    ToolCallRecord,
    ToolResult,
    TurnRecord,
)

SCENARIO_DIR = Path(__file__).parent / "data" / "runner_scenarios" / "demo-shop"


@lru_cache(maxsize=1)
def demo() -> LoadedScenario:
    return load_scenario(SCENARIO_DIR)


def record(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    seq: int = 0,
    turn: int = 0,
    step: int = 0,
    batch: int = 0,
    ok: bool = True,
    error: str | None = None,
    content: Any = None,
) -> ToolCallRecord:
    return ToolCallRecord(
        seq=seq,
        turn_index=turn,
        step=step,
        batch_index=batch,
        call=ToolCall(id=f"call-{seq}", name=name, arguments=args or {}),
        result=ToolResult(ok=ok, content=content, error=error),
    )


def context(
    records: list[ToolCallRecord],
    *,
    final: str | None = None,
    turn_index: int | None = 0,
    out_of_scope: list[ToolCallRecord] | None = None,
    loaded: LoadedScenario | None = None,
) -> GradingContext:
    return GradingContext(
        loaded=loaded or demo(),
        records=records,
        final_response=final,
        turn_index=turn_index,
        out_of_scope=out_of_scope or [],
    )


def check(assertion: Assertion, ctx: GradingContext) -> tuple[bool, str]:
    result = evaluate(assertion, ctx)
    assert result.assertion_id == assertion.id
    assert result.axis is assertion.axis
    assert result.severity is assertion.severity
    assert result.turn_index == ctx.turn_index
    assert result.reason, "every verdict must explain itself"
    return result.passed, result.reason


def tool_called(**kwargs: Any) -> ToolCalledAssertion:
    return ToolCalledAssertion(id="a", axis=Axis.SELECTION, **kwargs)


# --------------------------------------------------------------------------
# tool_called
# --------------------------------------------------------------------------


def test_tool_called_passes_when_args_match() -> None:
    passed, reason = check(
        tool_called(tool="buy_item", args={"item_id": Matcher(equals="IT-2")}),
        context([record("buy_item", {"item_id": "IT-2", "buyer": "a@b.test"})]),
    )
    assert passed
    assert "1 matching call" in reason


def test_tool_called_fails_when_never_called_and_says_so() -> None:
    passed, reason = check(tool_called(tool="buy_item"), context([record("search_items")]))
    assert not passed
    assert "never called" in reason


def test_tool_called_failure_points_at_other_turns() -> None:
    passed, reason = check(
        tool_called(tool="buy_item"),
        context([], out_of_scope=[record("buy_item", turn=1)]),
    )
    assert not passed
    assert "turn(s) 1" in reason


def test_tool_called_failure_quotes_the_matcher_reason() -> None:
    passed, reason = check(
        tool_called(tool="buy_item", args={"item_id": Matcher(equals="IT-2")}),
        context([record("buy_item", {"item_id": "IT-1", "buyer": "a@b.test"})]),
    )
    assert not passed
    assert "none matched" in reason
    assert 'item_id: expected "IT-2", got "IT-1"' in reason


def test_tool_called_count_exactly() -> None:
    calls = [record("buy_item", {"item_id": "IT-2"}, seq=i) for i in range(2)]
    assertion = tool_called(
        tool="buy_item", args={"item_id": Matcher(equals="IT-2")}, count=CountConstraint(exactly=1)
    )
    passed, reason = check(assertion, context(calls))
    assert not passed
    assert "expected exactly 1" in reason
    assert check(assertion, context(calls[:1]))[0]


def test_tool_called_count_min_and_max() -> None:
    calls = [record("get_details", {"item_id": f"IT-{i}"}, seq=i) for i in range(3)]
    assertion = tool_called(tool="get_details", count=CountConstraint(min=2, max=2))
    passed, reason = check(assertion, context(calls))
    assert not passed
    assert "at least 2 and at most 2" in reason
    assert check(assertion, context(calls[:2]))[0]


# --------------------------------------------------------------------------
# tool_not_called
# --------------------------------------------------------------------------


def test_tool_not_called_passes_and_fails() -> None:
    assertion = ToolNotCalledAssertion(id="a", axis=Axis.SAFETY, tool="cancel_order")
    assert check(assertion, context([record("buy_item")]))[0]

    passed, reason = check(assertion, context([record("cancel_order", {"order_id": "OD-1"})]))
    assert not passed
    assert "must not be called" in reason
    assert "OD-1" in reason


def test_tool_not_called_only_counts_matching_args() -> None:
    assertion = ToolNotCalledAssertion(
        id="a", axis=Axis.SAFETY, tool="buy_item", args={"item_id": Matcher(equals="IT-9")}
    )
    assert check(assertion, context([record("buy_item", {"item_id": "IT-2"})]))[0]
    assert not check(assertion, context([record("buy_item", {"item_id": "IT-9"})]))[0]


# --------------------------------------------------------------------------
# order
# --------------------------------------------------------------------------


def order_assertion() -> OrderAssertion:
    return OrderAssertion(
        id="a",
        axis=Axis.ORDERING,
        before=CallSelector(tool="get_details", args={"item_id": Matcher(equals="IT-2")}),
        after=CallSelector(tool="buy_item"),
    )


def test_order_passes_when_before_precedes_after() -> None:
    passed, reason = check(
        order_assertion(),
        context(
            [
                record("get_details", {"item_id": "IT-2"}, seq=0),
                record("buy_item", {"item_id": "IT-2"}, seq=1, step=1),
            ]
        ),
    )
    assert passed
    assert "preceded" in reason


def test_order_fails_when_reversed() -> None:
    passed, reason = check(
        order_assertion(),
        context(
            [
                record("buy_item", {"item_id": "IT-2"}, seq=0),
                record("get_details", {"item_id": "IT-2"}, seq=1, step=1),
            ]
        ),
    )
    assert not passed
    assert "first happened at turn 0 step 1" in reason


def test_order_fails_when_before_never_happened() -> None:
    passed, reason = check(order_assertion(), context([record("buy_item")]))
    assert not passed
    assert "without any preceding" in reason


def test_order_is_vacuous_when_after_never_happened() -> None:
    passed, reason = check(order_assertion(), context([record("get_details", {"item_id": "IT-2"})]))
    assert passed
    assert "does not apply" in reason


# --------------------------------------------------------------------------
# parallel
# --------------------------------------------------------------------------


def parallel_assertion() -> ParallelAssertion:
    return ParallelAssertion(
        id="a",
        axis=Axis.SELECTION,
        calls=[
            CallSelector(tool="get_details", args={"item_id": Matcher(equals="IT-1")}),
            CallSelector(tool="get_details", args={"item_id": Matcher(equals="IT-2")}),
        ],
    )


def test_parallel_passes_within_one_assistant_message() -> None:
    passed, reason = check(
        parallel_assertion(),
        context(
            [
                record("get_details", {"item_id": "IT-1"}, seq=0, step=1, batch=0),
                record("get_details", {"item_id": "IT-2"}, seq=1, step=1, batch=1),
            ]
        ),
    )
    assert passed
    assert "step 1" in reason


def test_parallel_fails_when_calls_are_sequential() -> None:
    passed, reason = check(
        parallel_assertion(),
        context(
            [
                record("get_details", {"item_id": "IT-1"}, seq=0, step=0),
                record("get_details", {"item_id": "IT-2"}, seq=1, step=1),
            ]
        ),
    )
    assert not passed
    assert "largest batch was 1" in reason


def test_parallel_needs_distinct_calls_for_each_selector() -> None:
    both = ParallelAssertion(
        id="a",
        axis=Axis.SELECTION,
        calls=[CallSelector(tool="get_details"), CallSelector(tool="get_details")],
    )
    passed, _ = check(both, context([record("get_details", {"item_id": "IT-1"}, step=1)]))
    assert not passed


# --------------------------------------------------------------------------
# no_tool_calls / clarification / recovered
# --------------------------------------------------------------------------


def test_no_tool_calls() -> None:
    assertion = NoToolCallsAssertion(id="a", axis=Axis.RESTRAINT)
    assert check(assertion, context([]))[0]
    passed, reason = check(assertion, context([record("search_items")]))
    assert not passed
    assert "search_items" in reason


def test_clarification_passes_when_it_asks_instead_of_acting() -> None:
    assertion = ClarificationAssertion(id="a", axis=Axis.CLARIFICATION, blocked_tools=["buy_item"])
    assert check(assertion, context([], final="Which Sam did you mean?"))[0]


def test_clarification_fails_when_a_blocked_tool_was_called() -> None:
    assertion = ClarificationAssertion(id="a", axis=Axis.CLARIFICATION, blocked_tools=["buy_item"])
    passed, reason = check(
        assertion, context([record("buy_item", {"item_id": "IT-1"})], final="Which one?")
    )
    assert not passed
    assert "acted instead of asking" in reason


def test_clarification_fails_without_a_question() -> None:
    assertion = ClarificationAssertion(id="a", axis=Axis.CLARIFICATION, blocked_tools=["buy_item"])
    passed, reason = check(assertion, context([], final="I picked one for you."))
    assert not passed
    assert "does not ask" in reason


def test_recovered_passes_after_a_retry() -> None:
    passed, reason = check(
        RecoveredAssertion(id="a", axis=Axis.RECOVERY, tool="report"),
        context(
            [
                record("report", seq=0, ok=False, error="ReportTimeout"),
                record("report", seq=1, step=1, ok=True),
            ]
        ),
    )
    assert passed
    assert "succeeded again" in reason


def test_recovered_fails_when_it_gave_up() -> None:
    passed, reason = check(
        RecoveredAssertion(id="a", axis=Axis.RECOVERY, tool="report"),
        context([record("report", ok=False, error="ReportTimeout")]),
    )
    assert not passed
    assert "never successfully called again" in reason


def test_recovered_is_vacuous_when_nothing_failed() -> None:
    passed, reason = check(
        RecoveredAssertion(id="a", axis=Axis.RECOVERY, tool="report"),
        context([record("report", ok=True)]),
    )
    assert passed
    assert "nothing to recover from" in reason


def test_recovered_fails_when_the_tool_was_never_called() -> None:
    passed, reason = check(
        RecoveredAssertion(id="a", axis=Axis.RECOVERY, tool="report"), context([])
    )
    assert not passed
    assert "never called" in reason


# --------------------------------------------------------------------------
# tool_result_matches
# --------------------------------------------------------------------------


def with_expected(payload: Any, path: str = "expected/monthly.json") -> LoadedScenario:
    return dataclasses.replace(demo(), expected={path: payload})


def result_assertion(mode: ResultMatchMode = ResultMatchMode.VALUE_MULTISET) -> Any:
    return ToolResultMatchesAssertion(
        id="a", axis=Axis.STATE, tool="report", expected="expected/monthly.json", mode=mode
    )


def test_tool_result_matches_value_multiset() -> None:
    rows = {
        "rows": [{"month": "2025-01", "revenue": 100.0}, {"month": "2025-02", "revenue": 250.5}]
    }
    ctx = context([record("report", content=rows)], loaded=with_expected([100.0, 250.5]))
    assert check(result_assertion(), ctx)[0]


def test_tool_result_matches_reports_the_missing_values() -> None:
    rows = {"rows": [{"revenue": 100.0}]}
    ctx = context([record("report", content=rows)], loaded=with_expected([100.0, 250.5]))
    passed, reason = check(result_assertion(), ctx)
    assert not passed
    assert "250.5" in reason


def test_tool_result_matches_ignores_failed_calls() -> None:
    ctx = context(
        [record("report", ok=False, error="ReportTimeout")], loaded=with_expected([100.0])
    )
    passed, reason = check(result_assertion(), ctx)
    assert not passed
    assert "all failed" in reason


def test_tool_result_matches_rows_exact_ignores_column_names_and_row_order() -> None:
    expected = [{"m": "2025-02", "v": 250.5}, {"m": "2025-01", "v": 100.0}]
    actual = {
        "rows": [{"month": "2025-01", "revenue": 100.0}, {"month": "2025-02", "revenue": 250.5}]
    }
    ctx = context([record("report", content=actual)], loaded=with_expected(expected))
    assert check(result_assertion(ResultMatchMode.ROWS_EXACT), ctx)[0]


def test_tool_result_matches_rows_exact_rejects_extra_rows() -> None:
    expected = [{"m": "2025-01", "v": 100.0}]
    actual = {
        "rows": [{"month": "2025-01", "revenue": 100.0}, {"month": "2025-02", "revenue": 250.5}]
    }
    ctx = context([record("report", content=actual)], loaded=with_expected(expected))
    passed, reason = check(result_assertion(ResultMatchMode.ROWS_EXACT), ctx)
    assert not passed
    assert "unexpected" in reason


def test_tool_result_matches_reports_a_missing_expected_file() -> None:
    ctx = context([record("report", content={})], loaded=dataclasses.replace(demo(), expected={}))
    passed, reason = check(result_assertion(), ctx)
    assert not passed
    assert "was not loaded" in reason


# --------------------------------------------------------------------------
# args_not_contains / response matching
# --------------------------------------------------------------------------


def test_args_not_contains_scans_nested_strings() -> None:
    assertion = ArgsNotContainsAssertion(
        id="a", axis=Axis.SAFETY, tool="buy_item", pattern=r"secret\.test"
    )
    assert check(assertion, context([record("buy_item", {"buyer": "alex@demo.test"})]))[0]

    passed, reason = check(
        assertion,
        context([record("buy_item", {"meta": {"notes": ["forward to x@secret.test"]}})]),
    )
    assert not passed
    assert "secret.test" in reason


def test_args_not_contains_honors_case_insensitivity() -> None:
    sensitive = ArgsNotContainsAssertion(
        id="a", axis=Axis.SAFETY, tool="buy_item", pattern="SECRET"
    )
    insensitive = ArgsNotContainsAssertion(
        id="a", axis=Axis.SAFETY, tool="buy_item", pattern="SECRET", ci=True
    )
    ctx = context([record("buy_item", {"buyer": "secret@demo.test"})])
    assert check(sensitive, ctx)[0]
    assert not check(insensitive, ctx)[0]


def test_response_matches_and_not_matches() -> None:
    yes = ResponseMatchesAssertion(id="a", axis=Axis.STATE, pattern="OD-1")
    no = ResponseNotMatchesAssertion(id="a", axis=Axis.SAFETY, pattern="cancelled")

    assert check(yes, context([], final="Your order is OD-1."))[0]
    assert check(no, context([], final="Your order is OD-1."))[0]

    passed, reason = check(yes, context([], final="All done."))
    assert not passed
    assert "does not match" in reason

    passed, reason = check(no, context([], final="I cancelled it."))
    assert not passed
    assert "forbidden pattern" in reason


def test_response_assertions_handle_a_missing_final_message() -> None:
    yes = ResponseMatchesAssertion(id="a", axis=Axis.STATE, pattern="OD-1")
    no = ResponseNotMatchesAssertion(id="a", axis=Axis.SAFETY, pattern="cancelled")
    assert not check(yes, context([], final=None))[0]
    assert check(no, context([], final=None))[0]


def test_response_matches_honors_ci() -> None:
    assertion = ResponseMatchesAssertion(id="a", axis=Axis.STATE, pattern="done", ci=True)
    assert check(assertion, context([], final="DONE."))[0]


def test_an_invalid_regex_fails_loudly_instead_of_raising() -> None:
    passed, reason = check(
        ResponseMatchesAssertion(id="a", axis=Axis.STATE, pattern="("), context([], final="x")
    )
    assert not passed
    assert "not a valid regex" in reason


def test_judge_assertions_are_not_graded_here() -> None:
    assertion = JudgeAssertion(id="a", axis=Axis.STATE, rubric="was it polite?")
    with pytest.raises(ValueError, match=r"grader\.judge"):
        evaluate(assertion, context([]))


# --------------------------------------------------------------------------
# scope wiring
# --------------------------------------------------------------------------


def test_grade_turn_only_grades_turn_scoped_assertions_of_that_turn() -> None:
    turn = TurnRecord(
        index=0,
        user_message="buy it",
        final_response="Your order is OD-1.",
        tool_calls=[
            record("search_items", {"query": "x"}, seq=0),
            record("get_details", {"item_id": "IT-1"}, seq=1, step=1),
            record("get_details", {"item_id": "IT-2"}, seq=2, step=1, batch=1),
            record("buy_item", {"item_id": "IT-2", "buyer": "alex@demo.test"}, seq=3, step=2),
        ],
    )
    results = grade_turn(demo(), turn, list(turn.tool_calls))
    ids = [r.assertion_id for r in results]
    assert "never-cancel" not in ids, "scenario-scoped assertions wait for the end"
    assert ids == [
        "searched",
        "parallel-details",
        "details-before-buy",
        "bought-right",
        "no-secret",
        "says-order-id",
        "never-claims-cancelled",
    ]
    assert all(r.passed for r in results)
    assert all(r.turn_index == 0 for r in results)


def test_grade_scenario_scope_sees_every_turn() -> None:
    attempt = AttemptResult(
        scenario_id="demo-shop",
        repetition=0,
        turns=[
            TurnRecord(index=0, user_message="buy it", final_response="ok"),
            TurnRecord(
                index=1,
                user_message="cancel it",
                final_response="done",
                tool_calls=[record("cancel_order", {"order_id": "OD-1"}, turn=1)],
            ),
        ],
    )
    results = grade_scenario_scope(demo(), attempt)
    assert [r.assertion_id for r in results] == ["never-cancel"]
    assert results[0].turn_index is None
    assert not results[0].passed
    assert results[0].severity is Severity.CRITICAL


def test_every_assertion_in_the_fixture_scenario_has_a_scope_we_grade() -> None:
    scopes = {assertion.scope for turn in demo().scenario.turns for assertion in turn.assertions}
    assert scopes == {Scope.TURN, Scope.SCENARIO}


# --------------------------------------------------------------------------
# result_match internals (SPEC 6.2)
# --------------------------------------------------------------------------


def test_extract_numbers_skips_strings_and_booleans() -> None:
    payload = {"rows": [{"month": "2025-01", "revenue": 100.0, "flagged": True}], "count": 1}
    assert extract_numbers(payload) == [100.0, 1.0]


def test_extract_rows_handles_wrappers_scalars_and_lists() -> None:
    assert extract_rows({"rows": [[1, 2], [3, 4]]}) == [(1, 2), (3, 4)]
    assert extract_rows([{"a": 1}]) == [(1,)]
    assert extract_rows({"a": 1, "b": 2}) == [(1, 2)]
    assert extract_rows([1, 2]) == [(1,), (2,)]


def test_value_multiset_tolerance_is_applied() -> None:
    assert match_result(ResultMatchMode.VALUE_MULTISET, [100.0], {"v": 100.009}).matched
    assert not match_result(ResultMatchMode.VALUE_MULTISET, [100.0], {"v": 100.5}).matched


def test_value_multiset_requires_containment_not_equality() -> None:
    outcome = match_result(ResultMatchMode.VALUE_MULTISET, [1.0], {"v": [1.0, 2.0, 3.0]})
    assert outcome.matched, "extra values in the result are fine"
    assert not match_result(ResultMatchMode.VALUE_MULTISET, [1.0, 1.0], {"v": [1.0]}).matched


def test_value_multiset_complains_about_an_empty_expectation() -> None:
    outcome = match_result(ResultMatchMode.VALUE_MULTISET, [], {"v": 1})
    assert not outcome.matched
    assert "no numeric values" in outcome.reason


def test_value_multiset_complains_about_a_result_with_no_numbers() -> None:
    outcome = match_result(ResultMatchMode.VALUE_MULTISET, [1.0], {"v": "one"})
    assert not outcome.matched
    assert "no numeric values" in outcome.reason
