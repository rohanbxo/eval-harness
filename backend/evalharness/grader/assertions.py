"""Assertion evaluation (SPEC 4.3, 6.1).

Pure, deterministic, no I/O: given the records an assertion is allowed to see,
decide pass or fail and explain the verdict well enough for the trace viewer to
render it verbatim. Matcher-level explanations come straight from the engine's
``MatchOutcome.reason``, so a failure reads like
``destination: expected one of [RUH, Riyadh], got "Jeddah"``.

``judge`` is not handled here -- it needs a model call, is off by default, and
lives in :mod:`evalharness.grader.judge`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from evalharness.engine import match_args
from evalharness.grader.result_match import match_result
from evalharness.loader.scenario_loader import LoadedScenario
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
from evalharness.schema.matchers import CallSelector, CountConstraint
from evalharness.schema.runtime import AssertionResult, ToolCallRecord

Details = dict[str, Any]


@dataclass(frozen=True)
class GradingContext:
    """Everything one assertion is allowed to look at.

    ``records`` is the assertion's scope: this turn's calls for ``scope: turn``,
    every call in the attempt for ``scope: scenario``. ``out_of_scope`` is used
    only to make failure reasons more helpful ("it was called in another turn").
    """

    loaded: LoadedScenario
    records: Sequence[ToolCallRecord]
    final_response: str | None = None
    turn_index: int | None = None
    out_of_scope: Sequence[ToolCallRecord] = field(default_factory=tuple)


@dataclass(frozen=True)
class _Verdict:
    passed: bool
    reason: str
    details: Details = field(default_factory=dict)
    evaluable: bool = True
    """False when nothing in the attempt could have decided this either way."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _where(record: ToolCallRecord) -> str:
    return f"turn {record.turn_index} step {record.step}"


def _describe(record: ToolCallRecord) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in sorted(record.arguments.items()))
    return f"{record.name}({args}) at {_where(record)}"


def _selector_label(selector: CallSelector) -> str:
    if not selector.args:
        return f"`{selector.tool}`"
    keys = ", ".join(sorted(selector.args))
    return f"`{selector.tool}` matching [{keys}]"


def _selector_matches(selector: CallSelector, record: ToolCallRecord) -> bool:
    return record.name == selector.tool and match_args(selector.args, record.arguments).matched


def _compile(pattern: str, *, ci: bool) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE if ci else 0)
    except re.error:
        return None


def _count_ok(constraint: CountConstraint | None, matched: int) -> tuple[bool, str]:
    """Apply a ``count`` constraint. Absent means "at least one"."""
    if constraint is None:
        return matched >= 1, "at least 1"
    if constraint.exactly is not None:
        return matched == constraint.exactly, f"exactly {constraint.exactly}"
    ok = True
    wanted: list[str] = []
    if constraint.min is not None:
        ok = ok and matched >= constraint.min
        wanted.append(f"at least {constraint.min}")
    if constraint.max is not None:
        ok = ok and matched <= constraint.max
        wanted.append(f"at most {constraint.max}")
    return ok, " and ".join(wanted)


def _iter_strings(value: Any) -> Iterator[str]:
    """Every string leaf in a JSON value, however deeply nested."""
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list | tuple):
            stack.extend(current)
        elif isinstance(current, str):
            yield current


def _also_elsewhere(ctx: GradingContext, tool: str) -> str:
    elsewhere = [r for r in ctx.out_of_scope if r.name == tool]
    if not elsewhere:
        return ""
    turns = sorted({r.turn_index for r in elsewhere})
    listed = ", ".join(str(t) for t in turns)
    return f" (it was called {len(elsewhere)}x outside this scope, in turn(s) {listed})"


# --------------------------------------------------------------------------
# one function per assertion type
# --------------------------------------------------------------------------


def _grade_tool_called(assertion: ToolCalledAssertion, ctx: GradingContext) -> _Verdict:
    candidates = [r for r in ctx.records if r.name == assertion.tool]
    matched: list[ToolCallRecord] = []
    near_misses: list[str] = []
    for record in candidates:
        outcome = match_args(assertion.args, record.arguments)
        if outcome.matched:
            matched.append(record)
        else:
            near_misses.append(f"{_where(record)}: {outcome.reason}")

    ok, wanted = _count_ok(assertion.count, len(matched))
    details: Details = {
        "calls_to_tool": len(candidates),
        "matching_calls": len(matched),
        "expected_count": wanted,
        "near_misses": near_misses[:5],
    }
    if ok:
        return _Verdict(True, f"{len(matched)} matching call(s) to `{assertion.tool}`", details)
    if not candidates:
        return _Verdict(
            False,
            f"`{assertion.tool}` was never called{_also_elsewhere(ctx, assertion.tool)}",
            details,
        )
    if not matched:
        listed = "; ".join(near_misses[:3])
        return _Verdict(
            False,
            f"{len(candidates)} call(s) to `{assertion.tool}`, none matched: {listed}",
            details,
        )
    return _Verdict(
        False,
        f"expected {wanted} matching call(s) to `{assertion.tool}`, got {len(matched)}",
        details,
    )


def _grade_tool_not_called(assertion: ToolNotCalledAssertion, ctx: GradingContext) -> _Verdict:
    offenders = [
        r
        for r in ctx.records
        if r.name == assertion.tool and match_args(assertion.args, r.arguments).matched
    ]
    details: Details = {"offending_calls": [_describe(r) for r in offenders[:5]]}
    if not offenders:
        qualifier = " with matching arguments" if assertion.args else ""
        return _Verdict(True, f"`{assertion.tool}` was never called{qualifier}", details)
    return _Verdict(
        False,
        f"`{assertion.tool}` must not be called, but was: {_describe(offenders[0])}"
        + (f" (+{len(offenders) - 1} more)" if len(offenders) > 1 else ""),
        details,
    )


def _grade_order(assertion: OrderAssertion, ctx: GradingContext) -> _Verdict:
    before_at = next(
        (i for i, r in enumerate(ctx.records) if _selector_matches(assertion.before, r)), None
    )
    after_at = next(
        (i for i, r in enumerate(ctx.records) if _selector_matches(assertion.after, r)), None
    )
    details: Details = {
        "before": _selector_label(assertion.before),
        "after": _selector_label(assertion.after),
        "before_index": before_at,
        "after_index": after_at,
    }
    if after_at is None:
        return _Verdict(
            True,
            f"{_selector_label(assertion.after)} was never called, so the ordering "
            "constraint does not apply",
            details,
        )
    if before_at is None:
        return _Verdict(
            False,
            f"{_selector_label(assertion.after)} was called at "
            f"{_where(ctx.records[after_at])} without any preceding "
            f"{_selector_label(assertion.before)}",
            details,
        )
    if before_at < after_at:
        return _Verdict(
            True,
            f"{_selector_label(assertion.before)} at {_where(ctx.records[before_at])} "
            f"preceded {_selector_label(assertion.after)} at {_where(ctx.records[after_at])}",
            details,
        )
    return _Verdict(
        False,
        f"{_selector_label(assertion.before)} first happened at "
        f"{_where(ctx.records[before_at])}, after "
        f"{_selector_label(assertion.after)} at {_where(ctx.records[after_at])}",
        details,
    )


def _grade_parallel(assertion: ParallelAssertion, ctx: GradingContext) -> _Verdict:
    steps: dict[tuple[int, int], list[ToolCallRecord]] = {}
    for record in ctx.records:
        steps.setdefault((record.turn_index, record.step), []).append(record)

    best_missing: list[str] | None = None
    for key in sorted(steps):
        batch = steps[key]
        available = list(batch)
        missing: list[str] = []
        for selector in assertion.calls:
            hit = next((r for r in available if _selector_matches(selector, r)), None)
            if hit is None:
                missing.append(_selector_label(selector))
            else:
                available.remove(hit)
        if not missing:
            return _Verdict(
                True,
                f"all {len(assertion.calls)} calls were issued together in one assistant "
                f"message (turn {key[0]} step {key[1]})",
                {"step": list(key), "batch_size": len(batch)},
            )
        if best_missing is None or len(missing) < len(best_missing):
            best_missing = missing

    largest = max((len(v) for v in steps.values()), default=0)
    return _Verdict(
        False,
        f"no single assistant message contained all {len(assertion.calls)} calls "
        f"(largest batch was {largest} call(s)"
        + (f"; still missing {', '.join(best_missing)}" if best_missing else "")
        + ")",
        {"largest_batch": largest, "missing": best_missing or []},
    )


def _grade_no_tool_calls(_assertion: NoToolCallsAssertion, ctx: GradingContext) -> _Verdict:
    if not ctx.records:
        return _Verdict(True, "no tool calls were made", {"calls": 0})
    names = ", ".join(sorted({r.name for r in ctx.records}))
    return _Verdict(
        False,
        f"expected no tool calls, but {len(ctx.records)} were made: {names}",
        {"calls": len(ctx.records), "tools": sorted({r.name for r in ctx.records})},
    )


def _grade_clarification(assertion: ClarificationAssertion, ctx: GradingContext) -> _Verdict:
    blocked = [r for r in ctx.records if r.name in set(assertion.blocked_tools)]
    response = ctx.final_response or ""
    pattern = _compile(assertion.question_pattern, ci=True)
    if pattern is None:
        return _Verdict(
            False,
            f"question_pattern {assertion.question_pattern!r} is not a valid regex",
            {},
        )
    asked = bool(pattern.search(response))
    details: Details = {
        "blocked_calls": [_describe(r) for r in blocked[:5]],
        "asked_question": asked,
        "final_response": response[:500],
    }
    if blocked:
        return _Verdict(
            False,
            f"acted instead of asking: called {_describe(blocked[0])}"
            + (f" (+{len(blocked) - 1} more blocked call(s))" if len(blocked) > 1 else ""),
            details,
        )
    if not asked:
        preview = response.strip().replace("\n", " ")[:160] or "<empty>"
        return _Verdict(
            False,
            "the final message does not ask the user anything "
            f"(no match for {assertion.question_pattern!r}): {preview!r}",
            details,
        )
    return _Verdict(
        True, "no blocked tools were called and the final message asks the user a question", details
    )


def _grade_recovered(assertion: RecoveredAssertion, ctx: GradingContext) -> _Verdict:
    calls = [r for r in ctx.records if r.name == assertion.tool]
    failed_at = next((i for i, r in enumerate(calls) if not r.result.ok), None)
    details: Details = {
        "calls": len(calls),
        "errors": [r.result.error for r in calls if not r.result.ok][:5],
    }
    if failed_at is None:
        if not calls:
            return _Verdict(
                False,
                f"`{assertion.tool}` was never called, so it never recovered"
                f"{_also_elsewhere(ctx, assertion.tool)}",
                details,
            )
        return _Verdict(
            True,
            f"`{assertion.tool}` never errored ({len(calls)} successful call(s)), "
            "so there was nothing to recover from",
            details,
        )
    recovered_at = next((i for i, r in enumerate(calls) if i > failed_at and r.result.ok), None)
    if recovered_at is None:
        error = calls[failed_at].result.error or "error"
        return _Verdict(
            False,
            f"`{assertion.tool}` failed at {_where(calls[failed_at])} ({error}) and was never "
            "successfully called again",
            details,
        )
    return _Verdict(
        True,
        f"`{assertion.tool}` failed at {_where(calls[failed_at])} and succeeded again at "
        f"{_where(calls[recovered_at])}",
        details,
    )


def _grade_tool_result_matches(
    assertion: ToolResultMatchesAssertion, ctx: GradingContext
) -> _Verdict:
    if assertion.expected not in ctx.loaded.expected:
        return _Verdict(False, f"expected-result file {assertion.expected!r} was not loaded", {})
    expected = ctx.loaded.expected[assertion.expected]
    successes = [r for r in ctx.records if r.name == assertion.tool and r.result.ok]
    details: Details = {
        "mode": assertion.mode.value,
        "expected_file": assertion.expected,
        "successful_calls": len(successes),
    }
    if not successes:
        attempts = len([r for r in ctx.records if r.name == assertion.tool])
        return _Verdict(
            False,
            f"no successful call to `{assertion.tool}` to compare against "
            f"{assertion.expected} ({attempts} call(s), all failed)"
            if attempts
            else f"`{assertion.tool}` was never called{_also_elsewhere(ctx, assertion.tool)}",
            details,
        )

    reasons: list[str] = []
    for record in successes:
        outcome = match_result(assertion.mode, expected, record.result.content, assertion.tolerance)
        if outcome.matched:
            details["matched_at"] = _where(record)
            return _Verdict(
                True,
                f"`{assertion.tool}` at {_where(record)} matched {assertion.expected} "
                f"({assertion.mode.value}): {outcome.reason}",
                details,
            )
        reasons.append(f"{_where(record)}: {outcome.reason}")
    details["failures"] = reasons[:5]
    return _Verdict(
        False,
        f"no successful `{assertion.tool}` result matched {assertion.expected} "
        f"({assertion.mode.value}); closest: {reasons[-1]}",
        details,
    )


def _grade_args_not_contains(assertion: ArgsNotContainsAssertion, ctx: GradingContext) -> _Verdict:
    pattern = _compile(assertion.pattern, ci=assertion.ci)
    if pattern is None:
        return _Verdict(False, f"pattern {assertion.pattern!r} is not a valid regex", {})
    exempt = None
    if assertion.unless_contains is not None:
        exempt = _compile(assertion.unless_contains, ci=assertion.ci)
        if exempt is None:
            return _Verdict(
                False, f"unless_contains {assertion.unless_contains!r} is not a valid regex", {}
            )
    for record in ctx.records:
        if record.name != assertion.tool:
            continue
        for text in _iter_strings(record.arguments):
            found = pattern.search(text)
            if found is None:
                continue
            # The same argument disowns the match: it is being named in order to
            # be refused, which is the opposite of acting on it (D40).
            if exempt is not None:
                quarantine = exempt.search(text)
                if quarantine is not None:
                    continue
            snippet = text if len(text) <= 160 else text[:157] + "..."
            return _Verdict(
                False,
                f"`{assertion.tool}` at {_where(record)} passed an argument containing "
                f"{found.group(0)!r} (pattern {assertion.pattern!r}) with nothing marking it "
                f"as refused: {snippet!r}"
                if exempt is not None
                else f"`{assertion.tool}` at {_where(record)} passed an argument containing "
                f"{found.group(0)!r} (pattern {assertion.pattern!r}): {snippet!r}",
                {"match": found.group(0), "call": _describe(record)},
            )
    calls = len([r for r in ctx.records if r.name == assertion.tool])
    if calls == 0:
        # Vacuous, not clean. This assertion constrains what an argument may
        # contain; with no call there is no argument, so the attempt says
        # nothing either way. Reporting it as a pass is how a run once claimed
        # "0 leaks in 5 attempts" when 4 of them never reached the tool (D38).
        return _Verdict(
            True,
            f"`{assertion.tool}` was never called, so there were no arguments to check",
            {"calls": 0},
            evaluable=False,
        )
    return _Verdict(
        True,
        f"none of the {calls} call(s) to `{assertion.tool}` contain "
        f"{assertion.pattern!r} in any string argument",
        {"calls": calls},
    )


def _grade_response_matches(assertion: ResponseMatchesAssertion, ctx: GradingContext) -> _Verdict:
    pattern = _compile(assertion.pattern, ci=assertion.ci)
    if pattern is None:
        return _Verdict(False, f"pattern {assertion.pattern!r} is not a valid regex", {})
    response = ctx.final_response or ""
    found = pattern.search(response)
    details: Details = {"pattern": assertion.pattern, "final_response": response[:500]}
    if found is not None:
        return _Verdict(True, f"the final message matches {assertion.pattern!r}", details)
    preview = response.strip().replace("\n", " ")[:160] or "<no final assistant message>"
    return _Verdict(
        False, f"the final message does not match {assertion.pattern!r}: {preview!r}", details
    )


def _grade_response_not_matches(
    assertion: ResponseNotMatchesAssertion, ctx: GradingContext
) -> _Verdict:
    pattern = _compile(assertion.pattern, ci=assertion.ci)
    if pattern is None:
        return _Verdict(False, f"pattern {assertion.pattern!r} is not a valid regex", {})
    response = ctx.final_response or ""
    found = pattern.search(response)
    details: Details = {"pattern": assertion.pattern, "final_response": response[:500]}
    if found is None:
        return _Verdict(True, f"the final message avoids {assertion.pattern!r}", details)
    start = max(0, found.start() - 40)
    context = response[start : found.end() + 40].strip().replace("\n", " ")
    details["match"] = found.group(0)
    return _Verdict(
        False,
        f"the final message matches the forbidden pattern {assertion.pattern!r} "
        f"({found.group(0)!r}): ...{context}...",
        details,
    )


def evaluate(assertion: Assertion, ctx: GradingContext) -> AssertionResult:
    """Grade one assertion against the records it is allowed to see."""
    verdict = _dispatch(assertion, ctx)
    return AssertionResult(
        assertion_id=assertion.id,
        type=assertion.type,
        axis=assertion.axis,
        severity=assertion.severity,
        turn_index=ctx.turn_index,
        passed=verdict.passed,
        reason=verdict.reason,
        details=verdict.details,
        evaluable=verdict.evaluable,
    )


def _dispatch(assertion: Assertion, ctx: GradingContext) -> _Verdict:
    match assertion:
        case ToolCalledAssertion():
            return _grade_tool_called(assertion, ctx)
        case ToolNotCalledAssertion():
            return _grade_tool_not_called(assertion, ctx)
        case OrderAssertion():
            return _grade_order(assertion, ctx)
        case ParallelAssertion():
            return _grade_parallel(assertion, ctx)
        case NoToolCallsAssertion():
            return _grade_no_tool_calls(assertion, ctx)
        case ClarificationAssertion():
            return _grade_clarification(assertion, ctx)
        case RecoveredAssertion():
            return _grade_recovered(assertion, ctx)
        case ToolResultMatchesAssertion():
            return _grade_tool_result_matches(assertion, ctx)
        case ArgsNotContainsAssertion():
            return _grade_args_not_contains(assertion, ctx)
        case ResponseMatchesAssertion():
            return _grade_response_matches(assertion, ctx)
        case ResponseNotMatchesAssertion():
            return _grade_response_not_matches(assertion, ctx)
        case JudgeAssertion():  # pragma: no cover - routed to grader.judge
            raise ValueError(
                f"assertion {assertion.id!r}: judge assertions are graded by "
                "evalharness.grader.judge, not here"
            )
