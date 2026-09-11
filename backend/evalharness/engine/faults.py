"""Scripted fault injection (SPEC 5.4).

A fault names a tool and a 1-indexed call count *for that tool within the
attempt*. When the counter matches, the scripted response replaces whatever the
fixture or handler would have produced, and counts as an error unless the
author says otherwise.
"""

from __future__ import annotations

from collections.abc import Iterable

from evalharness.engine.fixtures import error_label
from evalharness.schema.runtime import ToolResult
from evalharness.schema.scenario import Fault

__all__ = ["fault_result", "select_fault"]


def select_fault(faults: Iterable[Fault], tool_name: str, call_index: int) -> Fault | None:
    """The first fault scripted for ``tool_name`` on its ``call_index``-th call."""
    return next(
        (f for f in faults if f.tool == tool_name and f.on_call == call_index),
        None,
    )


def fault_result(fault: Fault) -> ToolResult:
    """Shape a fault's scripted response as a :class:`ToolResult`."""
    if fault.ok:
        return ToolResult(ok=True, content=fault.response)
    return ToolResult(
        ok=False,
        content=fault.response,
        error=error_label(fault.response, "injected_fault"),
    )
