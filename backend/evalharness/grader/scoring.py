"""Scoring (SPEC 6.3).

Per attempt: a pass/fail verdict and one score per axis. Per run: pass@1,
pass^k, mean axis scores, cost, latency percentiles and token totals.

Cost is deliberately fragile in one direction only: if any single model call has
unknown pricing, the aggregate is ``None`` rather than a partial sum that would
read as a complete one.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from evalharness.schema.enums import EventType, Severity
from evalharness.schema.runtime import AssertionResult, AttemptResult


def score_axes(results: Sequence[AssertionResult]) -> dict[str, float]:
    """Passed assertions / evaluable assertions per axis, all severities (SPEC 6.3).

    Only axes that actually have assertions appear, so a scenario is never
    penalized for an axis it does not test.

    Assertions the attempt could not decide either way are excluded rather than
    counted as passes (D38): a constraint on an argument of a tool that was
    never called would otherwise hand the axis a free point for work the model
    did not do. An axis whose every assertion was inconclusive is omitted, the
    same as an axis with no assertions -- absence of evidence, not a score.
    """
    totals: dict[str, int] = {}
    passed: dict[str, int] = {}
    for result in results:
        if not result.evaluable:
            continue
        axis = result.axis.value
        totals[axis] = totals.get(axis, 0) + 1
        passed[axis] = passed.get(axis, 0) + (1 if result.passed else 0)
    return {axis: passed[axis] / total for axis, total in sorted(totals.items()) if total}


#: z for a two-sided 95% interval.
WILSON_Z_95: float = 1.959963984540054


def wilson_interval(passed: int, total: int, z: float = WILSON_Z_95) -> tuple[float, float]:
    """95% Wilson score interval for a pass rate.

    Wilson rather than the normal approximation because eval runs are small: at
    k=5 over five scenarios a model has 25 observations, where the normal
    interval misbehaves badly near 0 and 1 (it can even extend past them).
    Wilson stays inside [0, 1] and does not collapse to zero width on a clean
    sweep -- 25/25 gives roughly [0.87, 1.0], which is the honest statement that
    a perfect small sample still does not prove perfection.

    Returns (0.0, 1.0) for no observations: maximal uncertainty, not false
    confidence.
    """
    if total <= 0:
        return (0.0, 1.0)
    proportion = passed / total
    denominator = 1 + z**2 / total
    center = (proportion + z**2 / (2 * total)) / denominator
    margin = (
        z / denominator * ((proportion * (1 - proportion) / total + z**2 / (4 * total**2)) ** 0.5)
    )
    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    # At p=0 and p=1 the bounds are analytically exactly 0 and 1; floating point
    # lands a few ulps short, which would make an all-pass run render as 0.9999.
    if passed == 0:
        low = 0.0
    if passed == total:
        high = 1.0
    return (low, high)


def intervals_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True when two intervals overlap, i.e. the gap is not significant.

    Overlapping 95% intervals mean the difference is not established at that
    level. (The converse does not hold: non-overlap is a conservative test, so
    this flags "not significant" rather than asserting significance.)
    """
    return a[0] <= b[1] and b[0] <= a[1]


def critical_failures(results: Iterable[AssertionResult]) -> list[AssertionResult]:
    return [r for r in results if not r.passed and r.severity is Severity.CRITICAL]


def required_failures(results: Iterable[AssertionResult]) -> list[AssertionResult]:
    return [r for r in results if not r.passed and r.severity is Severity.REQUIRED]


def attempt_passed(results: Sequence[AssertionResult]) -> bool:
    """No critical failures, and every ``required`` assertion passed (SPEC 6.3)."""
    return not critical_failures(results) and not required_failures(results)


def total_cost(values: Iterable[float | None]) -> float | None:
    """Sum of costs, or ``None`` if any part is unknown (SPEC 6.3: never guess)."""
    listed = list(values)
    if not listed or any(v is None for v in listed):
        return None
    return sum(v for v in listed if v is not None)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass(frozen=True)
class RunSummary:
    """Aggregate over one model x scenario-set x k run (SPEC 6.3).

    Rates are computed over *graded* attempts only. An attempt that errored --
    the provider gave up, a quota ran out, the harness raised -- is missing data
    rather than evidence about the model, so folding it into pass@1 would report
    an outage as a capability gap. ``coverage`` says how much of the run actually
    produced evidence, and ``incomplete`` is the flag every view should surface.
    """

    attempts: int
    graded_attempts: int
    errored_attempts: int
    coverage: float
    incomplete: bool
    passed_attempts: int
    pass_at_1: float
    pass_hat_k: float
    scenarios_scored: int
    scenarios_total: int
    k: int
    axis_scores: dict[str, float]
    per_scenario: dict[str, dict[str, float]]
    cost_usd: float | None
    input_tokens: int
    output_tokens: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    mean_steps_per_turn: float | None
    truncated_attempts: int = 0
    """Attempts holding a response that stopped on finish_reason=length (D35)."""

    def to_dict(self) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "graded_attempts": self.graded_attempts,
            "errored_attempts": self.errored_attempts,
            "coverage": self.coverage,
            "incomplete": self.incomplete,
            "scenarios_scored": self.scenarios_scored,
            "scenarios_total": self.scenarios_total,
            "passed_attempts": self.passed_attempts,
            "pass_at_1": self.pass_at_1,
            "pass_hat_k": self.pass_hat_k,
            "k": self.k,
            "axis_scores": self.axis_scores,
            "per_scenario": self.per_scenario,
            "cost_usd": self.cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "mean_steps_per_turn": self.mean_steps_per_turn,
            "truncated_attempts": self.truncated_attempts,
        }


def summarize_run(attempts: Sequence[AttemptResult], k: int) -> RunSummary:
    """Roll attempts up into the run-level metrics of SPEC 6.3.

    Only graded attempts feed the rates. Errored ones are counted, reported, and
    otherwise kept out of every average.
    """
    by_scenario: dict[str, list[AttemptResult]] = {}
    for attempt in attempts:
        by_scenario.setdefault(attempt.scenario_id, []).append(attempt)

    graded = [a for a in attempts if a.graded]
    errored = len(attempts) - len(graded)
    coverage = len(graded) / len(attempts) if attempts else 0.0

    passed = sum(1 for a in graded if a.passed)
    pass_at_1 = passed / len(graded) if graded else 0.0

    # pass^k asks "did this scenario pass every time?", which is only answerable
    # when every repetition actually ran. A scenario with a missing attempt is
    # excluded from both sides of the ratio rather than counted as a failure.
    complete_scenarios = {
        scenario_id: group
        for scenario_id, group in by_scenario.items()
        if len(group) >= k and all(a.graded for a in group)
    }
    all_pass = [s for s, group in complete_scenarios.items() if all(a.passed for a in group)]
    pass_hat_k = len(all_pass) / len(complete_scenarios) if complete_scenarios else 0.0

    latencies = [
        float(event.latency_ms)
        for attempt in graded
        for event in attempt.events
        if event.type is EventType.MODEL_RESPONSE and event.latency_ms is not None
    ]
    steps = [turn.steps for attempt in graded for turn in attempt.turns]

    axis_totals: dict[str, list[float]] = {}
    for attempt in graded:
        for axis, score in attempt.axis_scores.items():
            axis_totals.setdefault(axis, []).append(score)

    per_scenario: dict[str, dict[str, float]] = {}
    for scenario_id, group in sorted(by_scenario.items()):
        scenario_graded = [a for a in group if a.graded]
        per_scenario[scenario_id] = {
            "attempts": float(len(group)),
            "graded": float(len(scenario_graded)),
            "errored": float(len(group) - len(scenario_graded)),
            "coverage": len(scenario_graded) / len(group) if group else 0.0,
            "pass_at_1": (
                sum(1 for a in scenario_graded if a.passed) / len(scenario_graded)
                if scenario_graded
                else 0.0
            ),
            "complete": 1.0 if scenario_id in complete_scenarios else 0.0,
        }

    return RunSummary(
        attempts=len(attempts),
        graded_attempts=len(graded),
        errored_attempts=errored,
        truncated_attempts=sum(1 for a in attempts if a.truncated),
        coverage=coverage,
        incomplete=coverage < 1.0,
        passed_attempts=passed,
        pass_at_1=pass_at_1,
        pass_hat_k=pass_hat_k,
        scenarios_scored=len(complete_scenarios),
        scenarios_total=len(by_scenario),
        k=k,
        axis_scores={axis: statistics.fmean(v) for axis, v in sorted(axis_totals.items())},
        per_scenario=per_scenario,
        cost_usd=total_cost(a.cost_usd for a in graded),
        input_tokens=sum(a.input_tokens for a in attempts),
        output_tokens=sum(a.output_tokens for a in attempts),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        mean_steps_per_turn=statistics.fmean(steps) if steps else None,
    )
