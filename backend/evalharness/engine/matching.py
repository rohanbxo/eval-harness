"""Matcher evaluation (SPEC 4.4).

Pure and deterministic: no network, no wall clock, no I/O. Every failure carries a
human-readable reason, because the trace viewer renders those reasons verbatim::

    destination: expected one of [RUH, Riyadh], got "Jeddah"

Reasons quote the expectation and the observed value and nothing else -- the
``ci``/``partial`` modifiers are not annotated, so the rendered text stays
exactly as SPEC 4.4 specifies it.

The configuration models live in :mod:`evalharness.schema.matchers`; this module
only interprets them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import TypeGuard

from dateutil import parser as date_parser
from pydantic import JsonValue

from evalharness.schema.matchers import Matcher

__all__ = ["MatchOutcome", "match_args", "match_value"]


@dataclass(frozen=True)
class MatchOutcome:
    """The result of applying a matcher, with a reason when it did not match."""

    matched: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.matched


_MATCHED = MatchOutcome(True)


def _fail(label: str, detail: str) -> MatchOutcome:
    return MatchOutcome(False, f"{label}: {detail}" if label else detail)


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render(value: JsonValue) -> str:
    """JSON rendering used for the observed value: ``got "Jeddah"``, ``got 5``."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - JsonValue is always encodable
        return repr(value)


def _plain(value: JsonValue) -> str:
    """Rendering used inside expectation lists: bare strings, JSON for everything else."""
    return value if isinstance(value, str) else render(value)


def _type_name(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _got(value: JsonValue) -> str:
    return f"got {render(value)}"


def _got_typed(value: JsonValue) -> str:
    return f"got {render(value)} ({_type_name(value)})"


# --------------------------------------------------------------------------- #
# comparison primitives
# --------------------------------------------------------------------------- #


def _is_number(value: JsonValue) -> TypeGuard[int | float]:
    """JSON numbers only. ``True``/``False`` are ints in Python but not numbers here."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _deep_equal(left: JsonValue, right: JsonValue, *, ci: bool) -> bool:
    """Deep equality that keeps ``1`` and ``True`` distinct, unlike ``==``."""
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold() if ci else left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _deep_equal(a, b, ci=ci) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _deep_equal(left[k], right[k], ci=ci) for k in left
        )
    if isinstance(left, list | dict) or isinstance(right, list | dict):
        return False
    return bool(left == right)


def _child(matcher: Matcher, parent: Matcher) -> Matcher:
    """Propagate ``ci`` into a sub-matcher that did not set it explicitly."""
    if parent.ci and "ci" not in matcher.model_fields_set:
        return matcher.model_copy(update={"ci": True})
    return matcher


def describe(matcher: Matcher) -> str:
    """A short "expected ..." phrase for a matcher, used in composite reasons."""
    keys = matcher.primary_keys
    if keys == {"gte", "lte"}:
        return f"expected a number between {matcher.gte} and {matcher.lte}"
    key = next(iter(keys))
    match key:
        case "equals":
            return f"expected {render(matcher.equals)}"
        case "any_of":
            listed = ", ".join(_plain(v) for v in matcher.any_of or [])
            return f"expected one of [{listed}]"
        case "regex":
            kind = "a partial match for" if matcher.partial else "a full match of"
            return f"expected {kind} /{matcher.regex}/"
        case "contains":
            return f"expected to contain {render(matcher.contains)}"
        case "not_contains":
            return f"expected not to contain {render(matcher.not_contains)}"
        case "gte":
            return f"expected a number >= {matcher.gte}"
        case "lte":
            return f"expected a number <= {matcher.lte}"
        case "date_equals":
            return f"expected the date {matcher.date_equals}"
        case "datetime_equals":
            return f"expected the instant {matcher.datetime_equals}"
        case "includes_all":
            parts = "; ".join(describe(_child(m, matcher)) for m in matcher.includes_all or [])
            return f"expected an array including elements where [{parts}]"
        case "exists":
            return "expected the key to be present" if matcher.exists else "expected no such key"
        case _:  # "absent"
            return "expected no such key" if matcher.absent else "expected the key to be present"


# --------------------------------------------------------------------------- #
# individual matchers
# --------------------------------------------------------------------------- #


def _match_equals(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    if _deep_equal(matcher.equals, value, ci=matcher.ci):
        return _MATCHED
    return _fail(label, f"expected {render(matcher.equals)}, {_got(value)}")


def _match_any_of(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    options = matcher.any_of or []
    if any(_deep_equal(option, value, ci=matcher.ci) for option in options):
        return _MATCHED
    listed = ", ".join(_plain(option) for option in options)
    return _fail(label, f"expected one of [{listed}], {_got(value)}")


def _match_regex(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    pattern_source = matcher.regex or ""
    kind = "a partial match for" if matcher.partial else "a full match of"
    if not isinstance(value, str):
        return _fail(label, f"expected a string matching /{pattern_source}/, {_got_typed(value)}")
    try:
        pattern = re.compile(pattern_source, re.IGNORECASE if matcher.ci else 0)
    except re.error as exc:
        return _fail(label, f"invalid regex /{pattern_source}/: {exc}")
    hit = pattern.search(value) if matcher.partial else pattern.fullmatch(value)
    if hit is not None:
        return _MATCHED
    return _fail(label, f"expected {kind} /{pattern_source}/, {_got(value)}")


def _containment(needle: JsonValue, value: JsonValue, *, ci: bool) -> bool | None:
    """``True``/``False`` for containment, ``None`` when the types cannot be compared."""
    if isinstance(value, str):
        if not isinstance(needle, str):
            return None
        folded_value = value.casefold() if ci else value
        folded_needle = needle.casefold() if ci else needle
        return folded_needle in folded_value
    if isinstance(value, list):
        return any(_deep_equal(needle, item, ci=ci) for item in value)
    return None


def _match_contains(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    needle = matcher.contains
    held = _containment(needle, value, ci=matcher.ci)
    if held is None:
        return _fail(
            label,
            f"expected a string or array containing {render(needle)}, {_got_typed(value)}",
        )
    if held:
        return _MATCHED
    return _fail(label, f"expected to contain {render(needle)}, {_got(value)}")


def _match_not_contains(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    needle = matcher.not_contains
    held = _containment(needle, value, ci=matcher.ci)
    if held is None:
        return _fail(
            label,
            f"expected a string or array not containing {render(needle)}, {_got_typed(value)}",
        )
    if not held:
        return _MATCHED
    return _fail(label, f"expected not to contain {render(needle)}, {_got(value)}")


def _match_bounds(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    if _is_number(value):
        if matcher.gte is not None and float(value) < matcher.gte:
            return _fail(label, f"expected >= {matcher.gte}, {_got(value)}")
        if matcher.lte is not None and float(value) > matcher.lte:
            return _fail(label, f"expected <= {matcher.lte}, {_got(value)}")
        return _MATCHED
    bounds = []
    if matcher.gte is not None:
        bounds.append(f">= {matcher.gte}")
    if matcher.lte is not None:
        bounds.append(f"<= {matcher.lte}")
    return _fail(label, f"expected a number {' and '.join(bounds)}, {_got_typed(value)}")


def _parse_datetime(raw: JsonValue) -> datetime | None:
    """Parse an ISO 8601 date or datetime. Never consults the wall clock."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        # isoparse (not parse) on purpose: it is strict ISO 8601 and never falls
        # back to "today" for fields the string omits, so this stays clock-free.
        return date_parser.isoparse(raw.strip())
    except (ValueError, OverflowError):
        return None


def _parse_date(raw: JsonValue) -> date | None:
    parsed = _parse_datetime(raw)
    return parsed.date() if parsed is not None else None


def _match_date_equals(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    expected_raw = matcher.date_equals or ""
    expected = _parse_date(expected_raw)
    if expected is None:
        return _fail(label, f"date_equals is not an ISO date: {render(expected_raw)}")
    actual = _parse_date(value)
    if actual is None:
        return _fail(label, f"expected an ISO date, {_got_typed(value)}")
    if actual == expected:
        return _MATCHED
    return _fail(label, f"expected the date {expected.isoformat()}, got {actual.isoformat()}")


def _match_datetime_equals(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    expected_raw = matcher.datetime_equals or ""
    expected = _parse_datetime(expected_raw)
    if expected is None:
        return _fail(label, f"datetime_equals is not an ISO datetime: {render(expected_raw)}")
    actual = _parse_datetime(value)
    if actual is None:
        return _fail(label, f"expected an ISO datetime, {_got_typed(value)}")
    if (expected.tzinfo is None) != (actual.tzinfo is None):
        naive, offset = (
            (render(value), expected_raw)
            if actual.tzinfo is None
            else (expected_raw, render(value))
        )
        return _fail(
            label,
            f"cannot compare instants: {naive} has no UTC offset while {offset} does",
        )
    if expected == actual:
        return _MATCHED
    return _fail(label, f"expected the instant {expected.isoformat()}, got {actual.isoformat()}")


def _match_includes_all(matcher: Matcher, value: JsonValue, label: str) -> MatchOutcome:
    submatchers = matcher.includes_all or []
    if not isinstance(value, list):
        return _fail(label, f"expected an array, {_got_typed(value)}")
    # Greedy assignment: sub-matchers are satisfied in the order they are written
    # and each one consumes the first still-unused element it matches. Order-free
    # with respect to the array, but not a maximum-bipartite matching -- two
    # overlapping sub-matchers should be written specifically enough to separate.
    used: set[int] = set()
    for index, raw_sub in enumerate(submatchers):
        sub = _child(raw_sub, matcher)
        hit = next(
            (
                position
                for position, item in enumerate(value)
                if position not in used and match_value(sub, item, label="").matched
            ),
            None,
        )
        if hit is None:
            return _fail(
                label,
                f"no unused element matches includes_all[{index}] ({describe(sub)}), {_got(value)}",
            )
        used.add(hit)
    return _MATCHED


def _match_presence(matcher: Matcher, *, present: bool, label: str) -> MatchOutcome:
    """``exists`` / ``absent``, the only matchers that look at key presence."""
    if "exists" in matcher.primary_keys:
        want_present = bool(matcher.exists)
    else:
        want_present = not bool(matcher.absent)
    if present == want_present:
        return _MATCHED
    if want_present:
        return _fail(label, "expected the key to be present, but it is missing")
    return _fail(label, "expected no such key, but it is present")


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def match_value(
    matcher: Matcher,
    value: JsonValue,
    *,
    present: bool = True,
    label: str = "value",
) -> MatchOutcome:
    """Apply ``matcher`` to ``value``.

    ``present=False`` means the key was not supplied at all, which only
    ``exists``/``absent`` can act on -- every other matcher fails with a reason
    saying the value is missing. ``label`` prefixes the reason; pass ``""`` for
    an unprefixed reason.
    """
    keys = matcher.primary_keys
    if keys & {"exists", "absent"}:
        return _match_presence(matcher, present=present, label=label)
    if not present:
        return _fail(label, f"{describe(matcher)}, but the value is missing")
    if keys == {"gte", "lte"} or keys == {"gte"} or keys == {"lte"}:
        return _match_bounds(matcher, value, label)
    key = next(iter(keys))
    match key:
        case "equals":
            return _match_equals(matcher, value, label)
        case "any_of":
            return _match_any_of(matcher, value, label)
        case "regex":
            return _match_regex(matcher, value, label)
        case "contains":
            return _match_contains(matcher, value, label)
        case "not_contains":
            return _match_not_contains(matcher, value, label)
        case "date_equals":
            return _match_date_equals(matcher, value, label)
        case "datetime_equals":
            return _match_datetime_equals(matcher, value, label)
        case _:  # "includes_all" -- Matcher validation admits nothing else
            return _match_includes_all(matcher, value, label)


def match_args(
    matchers: Mapping[str, Matcher],
    args: Mapping[str, JsonValue],
) -> MatchOutcome:
    """Apply an ``args`` block to a call's arguments (SPEC 4.4).

    Arguments the block does not list are ignored. Every failing matcher
    contributes a reason; they are joined with ``"; "`` so a call that is wrong
    in two places says so.
    """
    reasons: list[str] = []
    for name, matcher in matchers.items():
        present = name in args
        value: JsonValue = args[name] if present else None
        outcome = match_value(matcher, value, present=present, label=name)
        if not outcome.matched:
            reasons.append(outcome.reason)
    if reasons:
        return MatchOutcome(False, "; ".join(reasons))
    return _MATCHED
