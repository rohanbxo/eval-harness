"""Scenario loading and validation (SPEC 4, 8.2).

This module is the *only* place scenario data exists as raw dicts. Everything
downstream consumes the Pydantic models hanging off ``LoadedScenario``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from evalharness.loader.errors import RegistryError, ScenarioValidationError
from evalharness.loader.hashing import compute_config_hash
from evalharness.schema.assertions import ToolResultMatchesAssertion
from evalharness.schema.registry import ModelRegistry
from evalharness.schema.scenario import Scenario
from evalharness.schema.tools import FixtureFile, FixtureMock, ToolDefinition
from evalharness.schema.transcript import Transcript


@dataclass(frozen=True)
class LoadedScenario:
    """A scenario directory, fully parsed and cross-validated."""

    scenario: Scenario
    tools: list[ToolDefinition]
    fixtures: dict[str, FixtureFile]
    """Keyed by the path exactly as written in ``mock.file``."""
    expected: dict[str, Any] = field(default_factory=dict)
    """Expected-result files for ``tool_result_matches``, keyed by the assertion's path."""
    directory: Path = field(default_factory=Path)
    config_hash: str = ""

    @property
    def id(self) -> str:
        return self.scenario.id

    def tool(self, name: str) -> ToolDefinition | None:
        return next((t for t in self.tools if t.name == name), None)

    @property
    def tool_names(self) -> set[str]:
        return {t.name for t in self.tools}

    def openai_tools(self) -> list[dict[str, Any]]:
        """Provider-facing tool schemas, with ``mock`` stripped (SPEC 4.2)."""
        return [t.to_openai_schema() for t in self.tools]

    def snapshot(self) -> dict[str, Any]:
        """A JSON copy of the whole definition, stored on the run (SPEC 8.2)."""
        return {
            "scenario": self.scenario.model_dump(mode="json"),
            "tools": [t.model_dump(mode="json") for t in self.tools],
            "fixtures": {k: v.model_dump(mode="json") for k, v in self.fixtures.items()},
            "expected": self.expected,
            "config_hash": self.config_hash,
        }


def _format_pydantic_error(exc: ValidationError, file: Path) -> ScenarioValidationError:
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first["loc"]) or "<root>"
    hint: str | None = None
    if first["type"] == "extra_forbidden":
        hint = "unknown key; scenario config rejects unrecognized keys to catch typos"
    elif first["type"] == "missing":
        hint = "required key is absent"
    return ScenarioValidationError(file, first["msg"], path=loc, hint=hint)


def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ScenarioValidationError(path, "file not found")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioValidationError(path, f"invalid YAML: {exc}") from exc


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise ScenarioValidationError(path, "file not found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScenarioValidationError(
            path, f"invalid JSON: {exc.msg}", path=f"line {exc.lineno} col {exc.colno}"
        ) from exc


def _load_tools(directory: Path, scenario: Scenario) -> list[ToolDefinition]:
    tools_path = directory / scenario.tools
    raw = _read_json(tools_path)
    if not isinstance(raw, list):
        raise ScenarioValidationError(
            tools_path, f"expected a list of tool definitions, got {type(raw).__name__}"
        )

    tools: list[ToolDefinition] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        try:
            tool = ToolDefinition.model_validate(entry)
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(p) for p in first["loc"])
            raise ScenarioValidationError(
                tools_path, first["msg"], path=f"[{index}].{loc}"
            ) from exc

        if tool.name in seen:
            raise ScenarioValidationError(
                tools_path, f"duplicate tool name {tool.name!r}", path=f"[{index}].name"
            )
        seen.add(tool.name)

        # A malformed JSON Schema would silently accept every argument, so this is
        # checked at load time rather than discovered mid-run (SPEC 4.2).
        try:
            Draft202012Validator.check_schema(tool.parameters)
        except SchemaError as exc:
            raise ScenarioValidationError(
                tools_path,
                f"tool {tool.name!r} has an invalid JSON Schema: {exc.message}",
                path=f"[{index}].parameters",
            ) from exc
        tools.append(tool)

    if not tools:
        raise ScenarioValidationError(tools_path, "a scenario needs at least one tool")
    return tools


def _load_fixtures(directory: Path, tools: list[ToolDefinition]) -> dict[str, FixtureFile]:
    fixtures: dict[str, FixtureFile] = {}
    for tool in tools:
        if not isinstance(tool.mock, FixtureMock) or tool.mock.file in fixtures:
            continue
        fixture_path = directory / tool.mock.file
        raw = _read_json(fixture_path)
        try:
            fixtures[tool.mock.file] = FixtureFile.model_validate(raw)
        except ValidationError as exc:
            raise _format_pydantic_error(exc, fixture_path) from exc
    return fixtures


def _load_expected(directory: Path, scenario: Scenario) -> dict[str, Any]:
    expected: dict[str, Any] = {}
    for turn in scenario.turns:
        for assertion in turn.assertions:
            if not isinstance(assertion, ToolResultMatchesAssertion):
                continue
            if assertion.expected in expected:
                continue
            expected[assertion.expected] = _read_json(directory / assertion.expected)
    return expected


def _cross_validate(directory: Path, scenario: Scenario, tools: list[ToolDefinition]) -> None:
    """Catch references to tools that do not exist, which would never fire at runtime."""
    names = {t.name for t in tools}
    scenario_path = directory / "scenario.yaml"

    def check(tool_name: str, where: str) -> None:
        if tool_name not in names:
            raise ScenarioValidationError(
                scenario_path,
                f"references unknown tool {tool_name!r}",
                path=where,
                hint="tools.json defines: " + ", ".join(sorted(names)),
            )

    for fault_index, fault in enumerate(scenario.faults):
        check(fault.tool, f"faults[{fault_index}].tool")

    for turn_index, turn in enumerate(scenario.turns):
        for assertion in turn.assertions:
            where = f"turns[{turn_index}].assertions[{assertion.id}]"
            tool_name = getattr(assertion, "tool", None)
            if isinstance(tool_name, str):
                check(tool_name, f"{where}.tool")
            for attr in ("before", "after"):
                selector = getattr(assertion, attr, None)
                if selector is not None:
                    check(selector.tool, f"{where}.{attr}.tool")
            for call_index, selector in enumerate(getattr(assertion, "calls", None) or []):
                check(selector.tool, f"{where}.calls[{call_index}].tool")
            for blocked in getattr(assertion, "blocked_tools", None) or []:
                check(blocked, f"{where}.blocked_tools")


def load_scenario(directory: Path) -> LoadedScenario:
    """Load and fully validate one scenario directory."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ScenarioValidationError(directory, "scenario directory not found")

    scenario_path = directory / "scenario.yaml"
    raw = _read_yaml(scenario_path)
    if not isinstance(raw, dict):
        raise ScenarioValidationError(
            scenario_path, f"expected a mapping at the top level, got {type(raw).__name__}"
        )
    try:
        scenario = Scenario.model_validate(raw)
    except ValidationError as exc:
        raise _format_pydantic_error(exc, scenario_path) from exc

    if scenario.id != directory.name:
        raise ScenarioValidationError(
            scenario_path,
            f"id {scenario.id!r} must equal the directory name {directory.name!r}",
            path="id",
        )

    tools = _load_tools(directory, scenario)
    _cross_validate(directory, scenario, tools)
    fixtures = _load_fixtures(directory, tools)
    expected = _load_expected(directory, scenario)

    return LoadedScenario(
        scenario=scenario,
        tools=tools,
        fixtures=fixtures,
        expected=expected,
        directory=directory,
        config_hash=compute_config_hash(directory),
    )


def discover_scenario_dirs(root: Path) -> list[Path]:
    """Scenario directories under ``root``, sorted by id."""
    root = Path(root)
    if not root.is_dir():
        raise ScenarioValidationError(root, "scenarios root not found")
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "scenario.yaml").is_file())


def load_all_scenarios(root: Path) -> dict[str, LoadedScenario]:
    """Load every scenario under ``root``, keyed by id."""
    return {d.name: load_scenario(d) for d in discover_scenario_dirs(root)}


def load_transcripts(directory: Path) -> dict[str, Transcript]:
    """Load ``transcripts/*.yaml`` for a scenario, keyed by file stem (SPEC 6.4)."""
    transcripts_dir = Path(directory) / "transcripts"
    if not transcripts_dir.is_dir():
        return {}
    out: dict[str, Transcript] = {}
    for path in sorted(transcripts_dir.glob("*.yaml")):
        raw = _read_yaml(path)
        try:
            out[path.stem] = Transcript.model_validate(raw)
        except ValidationError as exc:
            raise _format_pydantic_error(exc, path) from exc
    return out


def load_registry(path: Path) -> ModelRegistry:
    """Load the model registry (SPEC 7)."""
    path = Path(path)
    if not path.exists():
        raise RegistryError(f"{path}: model registry not found")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return ModelRegistry.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise RegistryError(f"{path} at {loc}: {first['msg']}") from exc
