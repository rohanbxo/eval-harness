"""Table-driven fixture dispatch (SPEC 5.2).

A fixture file is a list of ``{match, response}`` rows plus a ``default``. The
first row whose matchers accept the call's arguments wins and its ``response``
is returned verbatim; ``default`` answers everything else.

Whether a row counts as a success is declarative, not sniffed from the payload
(see ``docs/DECISIONS.md`` D1): ``FixtureResponse.ok`` defaults to true and
``FixtureFile.default_ok`` defaults to false.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from evalharness.engine.matching import match_args
from evalharness.schema.runtime import ToolResult
from evalharness.schema.tools import FixtureFile

__all__ = ["FixtureSelection", "error_label", "fixture_result", "select_fixture"]


@dataclass(frozen=True)
class FixtureSelection:
    """Which fixture row answered a call, for the trace viewer."""

    index: int | None
    """Row index in ``responses``, or ``None`` when the default was used."""
    response: JsonValue
    ok: bool

    @property
    def is_default(self) -> bool:
        return self.index is None


def select_fixture(fixture: FixtureFile, args: Mapping[str, JsonValue]) -> FixtureSelection:
    """First matching row, else the file's ``default`` (SPEC 5.2)."""
    for index, row in enumerate(fixture.responses):
        if match_args(row.match, args).matched:
            return FixtureSelection(index=index, response=row.response, ok=row.ok)
    return FixtureSelection(index=None, response=fixture.default, ok=fixture.default_ok)


def error_label(content: JsonValue, fallback: str) -> str:
    """Best-effort error code for a failed result: the payload's own ``error`` key."""
    if isinstance(content, dict):
        value = content.get("error")
        if isinstance(value, str) and value:
            return value
    return fallback


def fixture_result(fixture: FixtureFile, args: Mapping[str, JsonValue]) -> ToolResult:
    """Run a fixture and shape the answer as a :class:`ToolResult`."""
    selection = select_fixture(fixture, args)
    if selection.ok:
        return ToolResult(ok=True, content=selection.response)
    fallback = "no_matching_fixture" if selection.is_default else "tool_error"
    return ToolResult(
        ok=False,
        content=selection.response,
        error=error_label(selection.response, fallback),
    )
