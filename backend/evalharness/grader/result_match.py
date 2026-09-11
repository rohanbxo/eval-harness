"""Comparison strategies for ``tool_result_matches`` (SPEC 6.2).

Pure and deterministic, like everything else in the grader. Two modes:

``value_multiset``
    Pull every number out of the result, whatever shape it has, and require the
    expected numbers to be *contained* in that multiset. Column names, key
    order, row order and date formatting all stop mattering, which is what makes
    it usable for the data-analyst scenario where a dozen phrasings of the same
    SQL are equally correct.

``rows_exact``
    Row-for-row equality ignoring row order and column names, comparing values
    positionally.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from evalharness.schema.enums import ResultMatchMode

#: Keys a tool result is likely to wrap its rows in.
_ROW_KEYS: tuple[str, ...] = ("rows", "data", "results", "values", "records", "items")

Row = tuple[Any, ...]


@dataclass(frozen=True)
class ResultMatchOutcome:
    """Whether a result matched, and why not when it did not."""

    matched: bool
    reason: str = ""


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def extract_numbers(value: Any) -> list[float]:
    """Every numeric leaf in ``value``, in document order.

    Strings are deliberately not parsed: a date like ``"2025-03"`` must not turn
    into the numbers 2025 and 3 and quietly satisfy an expectation.
    """
    out: list[float] = []
    stack: list[Any] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list | tuple):
            stack.extend(reversed(list(current)))
        elif _is_number(current):
            out.append(float(current))
    return out


def _find_rows(value: Any) -> Any:
    """Dig past a wrapper object (``{"rows": [...], "truncated": false}``)."""
    if isinstance(value, dict):
        for key in _ROW_KEYS:
            if key in value:
                return value[key]
    return value


def extract_rows(value: Any) -> list[Row]:
    """Normalize a tool result into positional rows, ignoring column names."""
    payload = _find_rows(value)
    if isinstance(payload, dict):
        return [tuple(payload.values())]
    if not isinstance(payload, list):
        return [(payload,)]
    rows: list[Row] = []
    for item in payload:
        if isinstance(item, dict):
            rows.append(tuple(item.values()))
        elif isinstance(item, list | tuple):
            rows.append(tuple(item))
        else:
            rows.append((item,))
    return rows


def _fmt(value: Any) -> str:
    if _is_number(value):
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:g}"
    return repr(value)


def _fmt_many(values: Iterable[Any], limit: int = 6) -> str:
    listed = list(values)
    shown = ", ".join(_fmt(v) for v in listed[:limit])
    return shown + (f", ... (+{len(listed) - limit} more)" if len(listed) > limit else "")


def _contains_multiset(
    expected: Sequence[float], actual: Sequence[float], tolerance: float
) -> list[float]:
    """Return the expected values that could not be paired off, best match first."""
    remaining = list(actual)
    missing: list[float] = []
    for want in expected:
        best_index: int | None = None
        best_delta = tolerance
        for index, have in enumerate(remaining):
            delta = abs(have - want)
            if delta <= best_delta:
                best_index, best_delta = index, delta
        if best_index is None:
            missing.append(want)
        else:
            remaining.pop(best_index)
    return missing


def _values_equal(left: Any, right: Any, tolerance: float) -> bool:
    if _is_number(left) and _is_number(right):
        return abs(float(left) - float(right)) <= tolerance
    return bool(left == right)


def _row_equal(left: Row, right: Row, tolerance: float) -> bool:
    return len(left) == len(right) and all(
        _values_equal(a, b, tolerance) for a, b in zip(left, right, strict=True)
    )


def _match_value_multiset(expected: Any, actual: Any, tolerance: float) -> ResultMatchOutcome:
    want = extract_numbers(expected)
    have = extract_numbers(actual)
    if not want:
        return ResultMatchOutcome(False, "the expected-result file contains no numeric values")
    if not have:
        return ResultMatchOutcome(
            False,
            f"the tool result contains no numeric values; expected {len(want)} "
            f"(e.g. {_fmt_many(want, 3)})",
        )
    missing = _contains_multiset(want, have, tolerance)
    if not missing:
        return ResultMatchOutcome(
            True, f"all {len(want)} expected value(s) present (tolerance {tolerance:g})"
        )
    return ResultMatchOutcome(
        False,
        f"{len(missing)} of {len(want)} expected value(s) missing from the result: "
        f"[{_fmt_many(missing)}]; result had {len(have)} value(s): [{_fmt_many(have)}]",
    )


def _match_rows_exact(expected: Any, actual: Any, tolerance: float) -> ResultMatchOutcome:
    want = extract_rows(expected)
    have = extract_rows(actual)

    unmatched_actual = list(have)
    missing: list[Row] = []
    for row in want:
        for index, candidate in enumerate(unmatched_actual):
            if _row_equal(row, candidate, tolerance):
                unmatched_actual.pop(index)
                break
        else:
            missing.append(row)

    if not missing and not unmatched_actual:
        return ResultMatchOutcome(True, f"all {len(want)} row(s) matched, ignoring order")

    parts: list[str] = [f"expected {len(want)} row(s), result had {len(have)}"]
    if missing:
        parts.append(f"missing: [{_fmt_many([tuple(r) for r in missing], 3)}]")
    if unmatched_actual:
        parts.append(f"unexpected: [{_fmt_many([tuple(r) for r in unmatched_actual], 3)}]")
    return ResultMatchOutcome(False, "; ".join(parts))


def match_result(
    mode: ResultMatchMode, expected: Any, actual: Any, tolerance: float = 0.01
) -> ResultMatchOutcome:
    """Compare a tool result against an expected-result file (SPEC 6.2)."""
    if mode is ResultMatchMode.VALUE_MULTISET:
        return _match_value_multiset(expected, actual, tolerance)
    return _match_rows_exact(expected, actual, tolerance)
