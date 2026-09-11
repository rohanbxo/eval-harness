"""95% Wilson confidence intervals for pass@1.

Eval runs are small — k=5 over five scenarios is 25 observations — and at that
size a bare pass rate invites conclusions the data does not support. These tests
pin the properties that make the interval trustworthy near the boundaries, which
is exactly where eval results cluster.
"""

from __future__ import annotations

import pytest

from evalharness.grader.scoring import WILSON_Z_95, intervals_overlap, wilson_interval


def test_no_observations_means_maximal_uncertainty() -> None:
    """Zero data must read as "unknown", never as a confident zero."""
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_a_perfect_small_sample_does_not_claim_certainty() -> None:
    """25/25 is strong, but it is not proof; the lower bound must stay below 1."""
    low, high = wilson_interval(25, 25)
    assert high == 1.0
    assert 0.85 < low < 0.90


def test_a_perfect_tiny_sample_is_barely_informative() -> None:
    """3/3 is what k=3 on one scenario gives, and it says very little."""
    low, high = wilson_interval(3, 3)
    assert high == 1.0
    assert low < 0.5, "3/3 must not imply better than a coin flip at 95%"


def test_zero_passes_does_not_exclude_success() -> None:
    low, high = wilson_interval(0, 3)
    assert low == 0.0
    assert high > 0.5


@pytest.mark.parametrize(
    ("passed", "total"),
    [(0, 1), (1, 1), (0, 25), (25, 25), (1, 2), (12, 25), (24, 25)],
)
def test_bounds_stay_inside_zero_and_one(passed: int, total: int) -> None:
    """The normal approximation can escape [0, 1] near the edges; Wilson may not."""
    low, high = wilson_interval(passed, total)
    assert 0.0 <= low <= high <= 1.0


def test_the_interval_brackets_the_observed_rate() -> None:
    for passed, total in ((5, 25), (12, 25), (20, 25)):
        low, high = wilson_interval(passed, total)
        assert low <= passed / total <= high


def test_more_data_narrows_the_interval() -> None:
    """The same rate, observed more often, must say more."""
    narrow = wilson_interval(40, 50)
    wide = wilson_interval(4, 5)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_z_is_the_two_sided_95_percent_value() -> None:
    assert pytest.approx(1.96, abs=0.001) == WILSON_Z_95


# --------------------------------------------------------------------------- #
# Significance flagging                                                        #
# --------------------------------------------------------------------------- #


def test_a_small_gap_is_not_significant() -> None:
    """23/25 vs 25/25 looks like a gap and is not one at this sample size."""
    assert intervals_overlap(wilson_interval(25, 25), wilson_interval(23, 25)) is True


def test_a_large_gap_is_significant() -> None:
    assert intervals_overlap(wilson_interval(25, 25), wilson_interval(10, 25)) is False


def test_overlap_is_symmetric() -> None:
    a, b = wilson_interval(20, 25), wilson_interval(15, 25)
    assert intervals_overlap(a, b) == intervals_overlap(b, a)


def test_a_row_always_overlaps_itself() -> None:
    """The leader is compared against itself, and must never be flagged a gap."""
    interval = wilson_interval(18, 25)
    assert intervals_overlap(interval, interval) is True


def test_touching_intervals_count_as_overlapping() -> None:
    """Boundary case: treat exact contact as not-established, the safer reading."""
    assert intervals_overlap((0.2, 0.5), (0.5, 0.9)) is True


def test_an_unobserved_model_overlaps_everything() -> None:
    """A model with no graded attempts cannot be ranked against anyone."""
    assert intervals_overlap(wilson_interval(0, 0), wilson_interval(25, 25)) is True
