"""Matchers, the mock tool engine, fixtures, faults and handlers (SPEC 4.4, 5).

Everything here is pure and deterministic: no network, no wall clock. Time comes
from the scenario's ``clock``.
"""

from evalharness.engine.faults import fault_result, select_fault
from evalharness.engine.fixtures import (
    FixtureSelection,
    error_label,
    fixture_result,
    select_fixture,
)
from evalharness.engine.handlers import (
    MAX_ROWS,
    SCENARIO_DIR_KEY,
    HandlerFn,
    get_handler,
    handler_names,
    leading_sql_keyword,
    register_handler,
    split_sql_statements,
)
from evalharness.engine.matching import MatchOutcome, describe, match_args, match_value
from evalharness.engine.tool_engine import ExecutionOutcome, ToolEngine

__all__ = [
    "MAX_ROWS",
    "SCENARIO_DIR_KEY",
    "ExecutionOutcome",
    "FixtureSelection",
    "HandlerFn",
    "MatchOutcome",
    "ToolEngine",
    "describe",
    "error_label",
    "fault_result",
    "fixture_result",
    "get_handler",
    "handler_names",
    "leading_sql_keyword",
    "match_args",
    "match_value",
    "register_handler",
    "select_fault",
    "select_fixture",
    "split_sql_statements",
]
