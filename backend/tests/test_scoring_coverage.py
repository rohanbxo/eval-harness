"""Errored attempts are missing data, not evidence (SPEC 6.3 gap; DECISIONS D19).

A run that loses attempts to a provider outage or a quota must not report that
as the model failing. These tests pin the distinction: errored attempts are
counted and surfaced, excluded from every rate, and pass^k is withheld for any
scenario that did not run all k times.
"""

from __future__ import annotations

import pytest

from evalharness.grader.scoring import summarize_run
from evalharness.schema.enums import AttemptStatus
from evalharness.schema.runtime import AttemptResult


def attempt(
    scenario: str,
    repetition: int,
    *,
    passed: bool = False,
    errored: bool = False,
    axis: dict[str, float] | None = None,
    cost: float | None = 0.0,
) -> AttemptResult:
    return AttemptResult(
        scenario_id=scenario,
        repetition=repetition,
        passed=passed,
        errored=errored,
        error="provider gave up" if errored else None,
        axis_scores=axis or {},
        cost_usd=cost,
    )


# --------------------------------------------------------------------------- #
# The status itself                                                            #
# --------------------------------------------------------------------------- #


def test_errored_is_distinct_from_failed() -> None:
    """Both exist as separate members, and neither shadows the other's value."""
    values = [status.value for status in AttemptStatus]
    assert "errored" in values
    assert "failed" in values
    assert len(values) == len(set(values))


@pytest.mark.parametrize(
    ("status", "graded"),
    [
        (AttemptStatus.COMPLETED, True),
        (AttemptStatus.FAILED, True),
        (AttemptStatus.ERRORED, False),
        (AttemptStatus.CANCELLED, False),
        (AttemptStatus.QUEUED, False),
        (AttemptStatus.RUNNING, False),
    ],
)
def test_only_finished_verdicts_count_as_graded(status: AttemptStatus, graded: bool) -> None:
    assert status.is_graded is graded


def test_an_errored_attempt_reports_itself_ungraded() -> None:
    assert attempt("s", 0, errored=True).graded is False
    assert attempt("s", 0, passed=False).graded is True


# --------------------------------------------------------------------------- #
# pass@1 and coverage                                                          #
# --------------------------------------------------------------------------- #


def test_pass_at_1_ignores_errored_attempts() -> None:
    """Two passes and two outages is 100%, not 50%."""
    summary = summarize_run(
        [
            attempt("a", 0, passed=True),
            attempt("a", 1, passed=True),
            attempt("b", 0, errored=True),
            attempt("b", 1, errored=True),
        ],
        k=2,
    )
    assert summary.pass_at_1 == 1.0
    assert summary.graded_attempts == 2
    assert summary.errored_attempts == 2


def test_coverage_reports_how_much_actually_ran() -> None:
    summary = summarize_run([attempt("a", 0, passed=True), attempt("a", 1, errored=True)], k=2)
    assert summary.coverage == 0.5
    assert summary.incomplete is True


def test_a_full_run_is_not_marked_incomplete() -> None:
    summary = summarize_run([attempt("a", 0, passed=True), attempt("a", 1, passed=False)], k=2)
    assert summary.coverage == 1.0
    assert summary.incomplete is False
    assert summary.errored_attempts == 0
    assert summary.pass_at_1 == 0.5


def test_a_run_of_nothing_but_errors_reports_zero_not_a_crash() -> None:
    summary = summarize_run([attempt("a", 0, errored=True)], k=1)
    assert summary.pass_at_1 == 0.0
    assert summary.pass_hat_k == 0.0
    assert summary.coverage == 0.0
    assert summary.incomplete is True
    assert summary.scenarios_scored == 0


def test_an_empty_run_is_handled() -> None:
    summary = summarize_run([], k=3)
    assert summary.attempts == 0
    assert summary.coverage == 0.0
    assert summary.pass_at_1 == 0.0


# --------------------------------------------------------------------------- #
# pass^k                                                                       #
# --------------------------------------------------------------------------- #


def test_pass_hat_k_skips_a_scenario_that_did_not_run_k_times() -> None:
    """A scenario with a missing repetition leaves the ratio entirely -- it is
    neither a pass nor a failure, because nobody knows."""
    summary = summarize_run(
        [
            attempt("solid", 0, passed=True),
            attempt("solid", 1, passed=True),
            attempt("patchy", 0, passed=True),
            attempt("patchy", 1, errored=True),
        ],
        k=2,
    )
    assert summary.scenarios_total == 2
    assert summary.scenarios_scored == 1
    assert summary.pass_hat_k == 1.0  # 1 of 1 eligible, not 1 of 2


def test_a_scenario_that_ran_fully_and_flaked_scores_zero() -> None:
    summary = summarize_run(
        [attempt("flaky", 0, passed=True), attempt("flaky", 1, passed=False)], k=2
    )
    assert summary.scenarios_scored == 1
    assert summary.pass_hat_k == 0.0


def test_pass_hat_k_needs_all_k_repetitions_present() -> None:
    """Only two of three attempts exist, so the scenario cannot be judged."""
    summary = summarize_run(
        [attempt("short", 0, passed=True), attempt("short", 1, passed=True)], k=3
    )
    assert summary.scenarios_scored == 0
    assert summary.pass_hat_k == 0.0


def test_pass_hat_k_and_pass_at_1_disagree_when_a_model_is_flaky() -> None:
    """The whole reason pass^k exists: 2-of-3 is not the same as 3-of-3."""
    summary = summarize_run(
        [
            attempt("a", 0, passed=True),
            attempt("a", 1, passed=True),
            attempt("a", 2, passed=False),
        ],
        k=3,
    )
    assert summary.pass_at_1 == pytest.approx(2 / 3)
    assert summary.pass_hat_k == 0.0


# --------------------------------------------------------------------------- #
# Aggregates exclude errored attempts too                                      #
# --------------------------------------------------------------------------- #


def test_axis_scores_exclude_errored_attempts() -> None:
    """An attempt that never ran has no axis scores to contribute."""
    summary = summarize_run(
        [
            attempt("a", 0, passed=True, axis={"safety": 1.0}),
            attempt("a", 1, errored=True, axis={"safety": 0.0}),
        ],
        k=2,
    )
    assert summary.axis_scores["safety"] == 1.0


def test_cost_excludes_errored_attempts() -> None:
    summary = summarize_run(
        [
            attempt("a", 0, passed=True, cost=0.25),
            attempt("a", 1, errored=True, cost=None),
        ],
        k=2,
    )
    # The errored attempt's unknown cost must not poison the known total.
    assert summary.cost_usd == pytest.approx(0.25)


def test_tokens_still_count_every_attempt() -> None:
    """Tokens were genuinely spent even on an attempt that errored out."""
    spent = attempt("a", 0, errored=True)
    spent.input_tokens, spent.output_tokens = 900, 100
    summary = summarize_run([spent], k=1)
    assert summary.input_tokens == 900
    assert summary.output_tokens == 100


# --------------------------------------------------------------------------- #
# Per-scenario reporting                                                       #
# --------------------------------------------------------------------------- #


def test_per_scenario_carries_coverage_and_completeness() -> None:
    summary = summarize_run(
        [
            attempt("good", 0, passed=True),
            attempt("good", 1, passed=True),
            attempt("broken", 0, passed=True),
            attempt("broken", 1, errored=True),
        ],
        k=2,
    )
    good = summary.per_scenario["good"]
    broken = summary.per_scenario["broken"]

    assert good["coverage"] == 1.0
    assert good["complete"] == 1.0
    assert good["errored"] == 0.0

    assert broken["coverage"] == 0.5
    assert broken["complete"] == 0.0
    assert broken["errored"] == 1.0
    # Its pass@1 still describes the attempt that ran.
    assert broken["pass_at_1"] == 1.0


def test_summary_serializes_the_new_fields() -> None:
    """The worker stores this dict as jsonb; the UI reads these keys."""
    payload = summarize_run([attempt("a", 0, errored=True)], k=1).to_dict()
    for key in (
        "graded_attempts",
        "errored_attempts",
        "coverage",
        "incomplete",
        "scenarios_scored",
        "scenarios_total",
    ):
        assert key in payload, key
