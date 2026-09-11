"""Unit tests for the matcher language (SPEC 4.4).

Every matcher is exercised on both paths, and the *reason* text is asserted as
carefully as the boolean: the trace viewer renders these strings, so they are a
product surface, not debug output.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import JsonValue

from evalharness.engine.matching import MatchOutcome, describe, match_args, match_value, render
from evalharness.schema.matchers import Matcher


def m(**kwargs: Any) -> Matcher:
    """Build a matcher the way config does, so ``model_fields_set`` is realistic."""
    return Matcher.model_validate(kwargs)


def reason(matcher: Matcher, value: JsonValue, *, label: str = "value") -> str:
    outcome = match_value(matcher, value, label=label)
    assert not outcome.matched, f"expected {value!r} not to match {matcher!r}"
    return outcome.reason


def assert_matches(matcher: Matcher, value: JsonValue) -> None:
    outcome = match_value(matcher, value)
    assert outcome.matched, outcome.reason
    assert outcome.reason == ""


# --------------------------------------------------------------------------- #
# the spec's own example
# --------------------------------------------------------------------------- #


def test_spec_example_reason_is_reproduced_verbatim() -> None:
    """SPEC 4.4 quotes this exact string; the trace viewer is built around it."""
    matchers = {"destination": m(any_of=["RUH", "Riyadh"], ci=True)}
    outcome = match_args(matchers, {"destination": "Jeddah"})
    assert outcome.reason == 'destination: expected one of [RUH, Riyadh], got "Jeddah"'
    assert not outcome.matched


# --------------------------------------------------------------------------- #
# MatchOutcome
# --------------------------------------------------------------------------- #


def test_outcome_is_truthy_when_matched() -> None:
    assert bool(MatchOutcome(True))
    assert not bool(MatchOutcome(False, "nope"))


def test_matcher_requires_exactly_one_primary_key() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        m(equals="a", regex="a")
    with pytest.raises(ValueError, match="exactly one"):
        m(ci=True)


# --------------------------------------------------------------------------- #
# equals
# --------------------------------------------------------------------------- #


class TestEquals:
    def test_scalar_match(self) -> None:
        assert_matches(m(equals="RUH"), "RUH")
        assert_matches(m(equals=7), 7)
        assert_matches(m(equals=None), None)
        assert_matches(m(equals=True), True)

    def test_scalar_mismatch_reason(self) -> None:
        assert reason(m(equals="RUH"), "JED") == 'value: expected "RUH", got "JED"'
        assert reason(m(equals=7), 9) == "value: expected 7, got 9"
        assert reason(m(equals=None), 0) == "value: expected null, got 0"

    def test_booleans_are_not_numbers(self) -> None:
        assert reason(m(equals=1), True) == "value: expected 1, got true"
        assert reason(m(equals=True), 1) == "value: expected true, got 1"

    def test_deep_equality_on_containers(self) -> None:
        assert_matches(m(equals={"a": [1, 2]}), {"a": [1, 2]})
        assert reason(m(equals={"a": [1, 2]}), {"a": [1, 3]}) == (
            'value: expected {"a": [1, 2]}, got {"a": [1, 3]}'
        )
        assert reason(m(equals=[1, 2]), [1, 2, 3]).startswith("value: expected [1, 2], got")
        assert reason(m(equals={"a": 1}), {"b": 1}).startswith("value: expected")
        assert reason(m(equals=[1]), "1").startswith("value: expected [1]")
        assert reason(m(equals="1"), [1]).startswith('value: expected "1"')

    def test_case_insensitive(self) -> None:
        assert_matches(m(equals="RUH", ci=True), "ruh")
        assert_matches(m(equals=["RUH"], ci=True), ["ruh"])
        assert_matches(m(equals={"city": "Dubai"}, ci=True), {"city": "DUBAI"})
        assert reason(m(equals="RUH", ci=True), "jed") == 'value: expected "RUH", got "jed"'

    def test_case_sensitive_by_default(self) -> None:
        assert reason(m(equals="RUH"), "ruh") == 'value: expected "RUH", got "ruh"'


# --------------------------------------------------------------------------- #
# any_of
# --------------------------------------------------------------------------- #


class TestAnyOf:
    def test_match(self) -> None:
        assert_matches(m(any_of=["DXB", "Dubai"]), "Dubai")
        assert_matches(m(any_of=["DXB", "Dubai"], ci=True), "dxb")
        assert_matches(m(any_of=[1, 2, 3]), 2)

    def test_reason_lists_every_option(self) -> None:
        assert reason(m(any_of=["RUH", "Riyadh"]), "Jeddah") == (
            'value: expected one of [RUH, Riyadh], got "Jeddah"'
        )
        assert reason(m(any_of=[1, 2]), 5) == "value: expected one of [1, 2], got 5"
        assert reason(m(any_of=[]), "x") == 'value: expected one of [], got "x"'

    def test_non_string_options_render_as_json(self) -> None:
        assert reason(m(any_of=[{"a": 1}, None]), 3) == (
            'value: expected one of [{"a": 1}, null], got 3'
        )


# --------------------------------------------------------------------------- #
# regex
# --------------------------------------------------------------------------- #


class TestRegex:
    def test_full_match_by_default(self) -> None:
        assert_matches(m(regex=r"FL-\d+"), "FL-204")
        assert reason(m(regex=r"FL-\d+"), "book FL-204 now") == (
            r'value: expected a full match of /FL-\d+/, got "book FL-204 now"'
        )

    def test_partial(self) -> None:
        assert_matches(m(regex=r"FL-\d+", partial=True), "book FL-204 now")
        assert reason(m(regex=r"FL-\d+", partial=True), "no flight here") == (
            r'value: expected a partial match for /FL-\d+/, got "no flight here"'
        )

    def test_case_insensitive(self) -> None:
        assert_matches(m(regex="fl-204", ci=True), "FL-204")
        assert reason(m(regex="fl-204"), "FL-204") == (
            'value: expected a full match of /fl-204/, got "FL-204"'
        )

    def test_non_string_value(self) -> None:
        assert reason(m(regex="x"), 5) == "value: expected a string matching /x/, got 5 (number)"
        assert reason(m(regex="x"), None) == (
            "value: expected a string matching /x/, got null (null)"
        )
        assert reason(m(regex="x"), ["x"]) == (
            'value: expected a string matching /x/, got ["x"] (array)'
        )
        assert reason(m(regex="x"), {"a": 1}) == (
            'value: expected a string matching /x/, got {"a": 1} (object)'
        )
        assert reason(m(regex="x"), True) == (
            "value: expected a string matching /x/, got true (boolean)"
        )

    def test_invalid_pattern_explains_itself_instead_of_raising(self) -> None:
        assert reason(m(regex="[unclosed"), "x").startswith("value: invalid regex /[unclosed/:")


# --------------------------------------------------------------------------- #
# contains / not_contains
# --------------------------------------------------------------------------- #


class TestContains:
    def test_substring(self) -> None:
        assert_matches(m(contains="beach"), "a beach house")
        assert reason(m(contains="beach"), "a mountain hut") == (
            'value: expected to contain "beach", got "a mountain hut"'
        )

    def test_substring_case_insensitive(self) -> None:
        assert_matches(m(contains="BEACH", ci=True), "a beach house")
        assert reason(m(contains="BEACH"), "a beach house") == (
            'value: expected to contain "BEACH", got "a beach house"'
        )

    def test_array_membership(self) -> None:
        assert_matches(m(contains="wifi"), ["wifi", "pool"])
        assert_matches(m(contains="WIFI", ci=True), ["wifi", "pool"])
        assert_matches(m(contains={"a": 1}), [{"a": 1}])
        assert reason(m(contains="gym"), ["wifi", "pool"]) == (
            'value: expected to contain "gym", got ["wifi", "pool"]'
        )

    def test_uncomparable_types(self) -> None:
        assert reason(m(contains="a"), 5) == (
            'value: expected a string or array containing "a", got 5 (number)'
        )
        assert reason(m(contains=5), "abc") == (
            'value: expected a string or array containing 5, got "abc" (string)'
        )
        assert reason(m(contains="a"), None) == (
            'value: expected a string or array containing "a", got null (null)'
        )


class TestNotContains:
    def test_substring(self) -> None:
        assert_matches(m(not_contains="beach"), "a mountain hut")
        assert reason(m(not_contains="beach"), "a beach house") == (
            'value: expected not to contain "beach", got "a beach house"'
        )

    def test_array_membership(self) -> None:
        assert_matches(m(not_contains="gym"), ["wifi", "pool"])
        assert reason(m(not_contains="WIFI", ci=True), ["wifi"]) == (
            'value: expected not to contain "WIFI", got ["wifi"]'
        )

    def test_uncomparable_types(self) -> None:
        assert reason(m(not_contains="a"), 5) == (
            'value: expected a string or array not containing "a", got 5 (number)'
        )


# --------------------------------------------------------------------------- #
# gte / lte
# --------------------------------------------------------------------------- #


class TestBounds:
    def test_gte(self) -> None:
        assert_matches(m(gte=100), 100)
        assert_matches(m(gte=100), 100.5)
        assert reason(m(gte=100), 50) == "value: expected >= 100.0, got 50"

    def test_lte(self) -> None:
        assert_matches(m(lte=100), 100)
        assert reason(m(lte=100), 150.5) == "value: expected <= 100.0, got 150.5"

    def test_range(self) -> None:
        matcher = m(gte=1, lte=10)
        assert_matches(matcher, 5)
        assert reason(matcher, 0) == "value: expected >= 1.0, got 0"
        assert reason(matcher, 11) == "value: expected <= 10.0, got 11"

    def test_non_numeric_values(self) -> None:
        assert reason(m(gte=1), "5") == 'value: expected a number >= 1.0, got "5" (string)'
        assert reason(m(lte=1), None) == "value: expected a number <= 1.0, got null (null)"
        assert reason(m(gte=1, lte=10), []) == (
            "value: expected a number >= 1.0 and <= 10.0, got [] (array)"
        )

    def test_booleans_are_not_numbers(self) -> None:
        assert reason(m(gte=0), True) == "value: expected a number >= 0.0, got true (boolean)"


# --------------------------------------------------------------------------- #
# date_equals / datetime_equals
# --------------------------------------------------------------------------- #


class TestDateEquals:
    def test_plain_dates(self) -> None:
        assert_matches(m(date_equals="2026-03-06"), "2026-03-06")

    def test_datetime_values_compare_only_the_date_part(self) -> None:
        assert_matches(m(date_equals="2026-03-06"), "2026-03-06T23:30:00+04:00")
        assert_matches(m(date_equals="2026-03-06T09:00:00Z"), "2026-03-06")

    def test_mismatch_reason(self) -> None:
        assert reason(m(date_equals="2026-03-06"), "2026-03-07") == (
            "value: expected the date 2026-03-06, got 2026-03-07"
        )

    def test_unparseable_value(self) -> None:
        assert reason(m(date_equals="2026-03-06"), "next friday") == (
            'value: expected an ISO date, got "next friday" (string)'
        )
        assert reason(m(date_equals="2026-03-06"), 20260306) == (
            "value: expected an ISO date, got 20260306 (number)"
        )
        assert reason(m(date_equals="2026-03-06"), "  ") == (
            'value: expected an ISO date, got "  " (string)'
        )

    def test_unparseable_expectation_blames_the_config(self) -> None:
        assert reason(m(date_equals="friday"), "2026-03-06") == (
            'value: date_equals is not an ISO date: "friday"'
        )


class TestDatetimeEquals:
    def test_same_instant_across_offsets(self) -> None:
        assert_matches(m(datetime_equals="2026-03-02T09:00:00+04:00"), "2026-03-02T05:00:00Z")
        assert_matches(m(datetime_equals="2026-03-02T05:00:00+00:00"), "2026-03-02T09:00:00+04:00")

    def test_different_instants(self) -> None:
        got = reason(m(datetime_equals="2026-03-02T09:00:00+04:00"), "2026-03-02T09:00:00Z")
        assert got == (
            "value: expected the instant 2026-03-02T09:00:00+04:00, got 2026-03-02T09:00:00+00:00"
        )

    def test_naive_values_are_rejected_against_an_offset_expectation(self) -> None:
        got = reason(m(datetime_equals="2026-03-02T09:00:00+04:00"), "2026-03-02T09:00:00")
        assert got == (
            'value: cannot compare instants: "2026-03-02T09:00:00" has no UTC offset '
            "while 2026-03-02T09:00:00+04:00 does"
        )

    def test_naive_expectation_against_an_aware_value(self) -> None:
        got = reason(m(datetime_equals="2026-03-02T09:00:00"), "2026-03-02T09:00:00+04:00")
        assert got == (
            "value: cannot compare instants: 2026-03-02T09:00:00 has no UTC offset "
            'while "2026-03-02T09:00:00+04:00" does'
        )

    def test_both_naive_compare_directly(self) -> None:
        assert_matches(m(datetime_equals="2026-03-02T09:00:00"), "2026-03-02T09:00:00")

    def test_unparseable(self) -> None:
        assert reason(m(datetime_equals="2026-03-02T09:00:00Z"), "tomorrow at nine") == (
            'value: expected an ISO datetime, got "tomorrow at nine" (string)'
        )
        assert reason(m(datetime_equals="whenever"), "2026-03-02T09:00:00Z") == (
            'value: datetime_equals is not an ISO datetime: "whenever"'
        )


# --------------------------------------------------------------------------- #
# includes_all
# --------------------------------------------------------------------------- #


class TestIncludesAll:
    def test_order_free_match(self) -> None:
        matcher = m(includes_all=[{"equals": "wifi"}, {"equals": "pool"}])
        assert_matches(matcher, ["pool", "gym", "wifi"])

    def test_each_submatcher_consumes_its_own_element(self) -> None:
        matcher = m(includes_all=[{"equals": "wifi"}, {"equals": "wifi"}])
        assert_matches(matcher, ["wifi", "wifi"])
        assert reason(matcher, ["wifi"]) == (
            'value: no unused element matches includes_all[1] (expected "wifi"), got ["wifi"]'
        )

    def test_missing_element_names_the_submatcher(self) -> None:
        matcher = m(includes_all=[{"equals": "wifi"}, {"gte": 3}])
        assert reason(matcher, ["wifi", 1]) == (
            "value: no unused element matches includes_all[1] "
            '(expected a number >= 3.0), got ["wifi", 1]'
        )

    def test_sub_matchers_can_be_any_matcher(self) -> None:
        matcher = m(includes_all=[{"regex": r"FL-\d+"}, {"contains": "pool"}])
        assert_matches(matcher, ["FL-204", "big pool area"])

    def test_ci_propagates_to_sub_matchers(self) -> None:
        assert_matches(m(includes_all=[{"equals": "WIFI"}], ci=True), ["wifi"])

    def test_explicit_sub_matcher_ci_wins(self) -> None:
        matcher = m(includes_all=[{"equals": "WIFI", "ci": False}], ci=True)
        assert reason(matcher, ["wifi"]).startswith("value: no unused element matches")

    def test_non_array_value(self) -> None:
        assert reason(m(includes_all=[{"equals": "wifi"}]), "wifi") == (
            'value: expected an array, got "wifi" (string)'
        )

    def test_empty_sub_matcher_list_matches_anything_array(self) -> None:
        assert_matches(m(includes_all=[]), [])


# --------------------------------------------------------------------------- #
# exists / absent
# --------------------------------------------------------------------------- #


class TestPresence:
    def test_exists_true(self) -> None:
        assert match_value(m(exists=True), "x", present=True).matched
        assert match_value(m(exists=True), None, present=True).matched
        outcome = match_value(m(exists=True), None, present=False, label="date")
        assert outcome.reason == "date: expected the key to be present, but it is missing"

    def test_exists_false(self) -> None:
        assert match_value(m(exists=False), None, present=False).matched
        outcome = match_value(m(exists=False), "x", present=True, label="cabin")
        assert outcome.reason == "cabin: expected no such key, but it is present"

    def test_absent_true(self) -> None:
        assert match_value(m(absent=True), None, present=False).matched
        outcome = match_value(m(absent=True), "x", present=True, label="cabin")
        assert outcome.reason == "cabin: expected no such key, but it is present"

    def test_absent_false(self) -> None:
        assert match_value(m(absent=False), "x", present=True).matched
        outcome = match_value(m(absent=False), None, present=False, label="cabin")
        assert outcome.reason == "cabin: expected the key to be present, but it is missing"


def test_missing_value_fails_every_other_matcher_with_its_expectation() -> None:
    assert match_value(m(equals="RUH"), None, present=False, label="dest").reason == (
        'dest: expected "RUH", but the value is missing'
    )
    assert match_value(m(gte=1, lte=9), None, present=False).reason == (
        "value: expected a number between 1.0 and 9.0, but the value is missing"
    )


# --------------------------------------------------------------------------- #
# describe()
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("matcher", "expected"),
    [
        (m(equals="RUH"), 'expected "RUH"'),
        (m(any_of=["a", 1]), "expected one of [a, 1]"),
        (m(regex="x"), "expected a full match of /x/"),
        (m(regex="x", partial=True), "expected a partial match for /x/"),
        (m(contains="a"), 'expected to contain "a"'),
        (m(not_contains="a"), 'expected not to contain "a"'),
        (m(gte=1), "expected a number >= 1.0"),
        (m(lte=1), "expected a number <= 1.0"),
        (m(gte=1, lte=2), "expected a number between 1.0 and 2.0"),
        (m(date_equals="2026-03-06"), "expected the date 2026-03-06"),
        (m(datetime_equals="2026-03-06T00:00:00Z"), "expected the instant 2026-03-06T00:00:00Z"),
        (m(exists=True), "expected the key to be present"),
        (m(exists=False), "expected no such key"),
        (m(absent=True), "expected no such key"),
        (m(absent=False), "expected the key to be present"),
        (
            m(includes_all=[{"equals": "a"}, {"gte": 2}]),
            'expected an array including elements where [expected "a"; expected a number >= 2.0]',
        ),
    ],
)
def test_describe(matcher: Matcher, expected: str) -> None:
    assert describe(matcher) == expected


def test_render_uses_json() -> None:
    assert render("a") == '"a"'
    assert render(None) == "null"
    assert render({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert render("café") == '"café"'


# --------------------------------------------------------------------------- #
# match_args
# --------------------------------------------------------------------------- #


class TestMatchArgs:
    def test_empty_block_matches_everything(self) -> None:
        assert match_args({}, {"anything": 1}).matched

    def test_unlisted_arguments_are_ignored(self) -> None:
        matchers = {"origin": m(equals="DXB")}
        assert match_args(matchers, {"origin": "DXB", "cabin": "business"}).matched

    def test_all_failures_are_reported(self) -> None:
        matchers = {"origin": m(equals="DXB"), "destination": m(equals="RUH")}
        outcome = match_args(matchers, {"origin": "AUH", "destination": "JED"})
        assert outcome.reason == (
            'origin: expected "DXB", got "AUH"; destination: expected "RUH", got "JED"'
        )

    def test_missing_argument(self) -> None:
        outcome = match_args({"date": m(date_equals="2026-03-06")}, {"origin": "DXB"})
        assert outcome.reason == "date: expected the date 2026-03-06, but the value is missing"

    def test_null_argument_is_present(self) -> None:
        assert match_args({"cabin": m(exists=True)}, {"cabin": None}).matched
        assert not match_args({"cabin": m(absent=True)}, {"cabin": None}).matched

    def test_absent_matcher_passes_when_the_key_is_omitted(self) -> None:
        assert match_args({"cabin": m(absent=True)}, {"origin": "DXB"}).matched
