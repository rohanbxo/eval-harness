"""The mock tool engine (SPEC 5.1).

One engine instance serves one loaded scenario. ``execute`` is the whole
contract, and it never raises: a hallucinated tool name, malformed arguments or
a broken handler are all *graded behaviors*, so they come back as ``ok=False``
results the model can read and react to.

Order of operations, per SPEC 5.1:

1. unknown tool name  -> ``unknown_tool``
2. JSON Schema check  -> ``invalid_arguments``
3. scripted fault     -> the fault's response (SPEC 5.4)
4. fixture or handler -> the mocked answer

The per-tool call counter that faults key off is incremented for *every* call to
that tool, including rejected and faulted ones (``docs/DECISIONS.md`` D7).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import JsonValue

from evalharness.engine.faults import fault_result, select_fault
from evalharness.engine.fixtures import fixture_result
from evalharness.engine.handlers import SCENARIO_DIR_KEY, close_connections, get_handler
from evalharness.loader.scenario_loader import LoadedScenario
from evalharness.schema.runtime import ToolResult
from evalharness.schema.scenario import Fault
from evalharness.schema.tools import FixtureMock, HandlerMock, ToolDefinition

__all__ = ["ExecutionOutcome", "ToolEngine"]


@dataclass(frozen=True)
class ExecutionOutcome:
    """What ``execute`` produced, plus the fault that caused it (if any)."""

    result: ToolResult
    faulted: bool = False
    fault: Fault | None = None


def _schema_errors(
    validator: Draft202012Validator, args: Mapping[str, JsonValue]
) -> list[JsonValue]:
    """Every JSON Schema violation, as ``{path, message}`` records the model can read."""
    details: list[JsonValue] = []
    for error in sorted(validator.iter_errors(dict(args)), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(part) for part in error.absolute_path)
        details.append({"path": path or "<root>", "message": str(error.message)})
    return details


class ToolEngine:
    """Executes mocked tool calls for one scenario."""

    def __init__(self, loaded: LoadedScenario) -> None:
        self._loaded = loaded
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in loaded.tools}
        self._validators: dict[str, Draft202012Validator] = {
            name: Draft202012Validator(tool.parameters) for name, tool in self._tools.items()
        }
        self._faults: list[Fault] = list(loaded.scenario.faults)
        self.state: dict[str, Any] = {}
        self.call_counts: dict[str, int] = {}

    # -- lifecycle ---------------------------------------------------------- #

    def reset(self) -> None:
        """Drop all per-attempt state. Called between attempts."""
        close_connections(self.state)
        self.state = {}
        self.call_counts = {}

    @property
    def tool_names(self) -> set[str]:
        return set(self._tools)

    def calls_to(self, tool_name: str) -> int:
        """How many times ``tool_name`` has been called in this attempt."""
        return self.call_counts.get(tool_name, 0)

    # -- execution ---------------------------------------------------------- #

    def execute(self, tool_name: str, args: Mapping[str, JsonValue]) -> ExecutionOutcome:
        """Run one tool call. Never raises (SPEC 5.1)."""
        tool = self._tools.get(tool_name)
        if tool is None:
            known = ", ".join(sorted(self._tools))
            return ExecutionOutcome(
                ToolResult(
                    ok=False,
                    error="unknown_tool",
                    content={
                        "error": "unknown_tool",
                        "message": f"No tool named {tool_name!r}. Available tools: {known}.",
                    },
                )
            )

        call_index = self.call_counts.get(tool_name, 0) + 1
        self.call_counts[tool_name] = call_index

        details = _schema_errors(self._validators[tool_name], args)
        if details:
            return ExecutionOutcome(
                ToolResult(
                    ok=False,
                    error="invalid_arguments",
                    content={"error": "invalid_arguments", "details": details},
                )
            )

        fault = select_fault(self._faults, tool_name, call_index)
        if fault is not None:
            return ExecutionOutcome(fault_result(fault), faulted=True, fault=fault)

        return ExecutionOutcome(self._dispatch(tool, args))

    def _dispatch(self, tool: ToolDefinition, args: Mapping[str, JsonValue]) -> ToolResult:
        mock = tool.mock
        if isinstance(mock, FixtureMock):
            fixture = self._loaded.fixtures.get(mock.file)
            if fixture is None:  # pragma: no cover - the loader guarantees this
                return ToolResult(
                    ok=False,
                    error="fixture_missing",
                    content={"error": "fixture_missing", "message": f"no fixture {mock.file!r}"},
                )
            return fixture_result(fixture, args)
        return self._run_handler(mock, args)

    def _run_handler(self, mock: HandlerMock, args: Mapping[str, JsonValue]) -> ToolResult:
        try:
            handler = get_handler(mock.name)
        except KeyError as exc:
            return ToolResult(
                ok=False,
                error="unknown_handler",
                content={"error": "unknown_handler", "message": str(exc.args[0])},
            )
        config: dict[str, JsonValue] = dict(mock.config)
        config[SCENARIO_DIR_KEY] = str(self._loaded.directory.resolve())
        try:
            return handler(args, self.state, config)
        except Exception as exc:  # a broken handler must not kill the attempt
            return ToolResult(
                ok=False,
                error="handler_error",
                content={
                    "error": "handler_error",
                    "message": f"{mock.name} raised {type(exc).__name__}: {exc}",
                },
            )
