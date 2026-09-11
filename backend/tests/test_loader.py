"""Loader tests (SPEC 4, 13 Phase 1 acceptance).

Every deliberately broken scenario under ``tests/data/broken/`` must fail with an
error that names the file, the path inside it, and the reason -- an author has to
be able to fix the problem without opening the loader.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evalharness.loader import (
    ScenarioValidationError,
    compute_config_hash,
    discover_scenario_dirs,
    load_all_scenarios,
    load_scenario,
    load_transcripts,
)
from evalharness.schema.tools import FixtureMock


@pytest.fixture(scope="session")
def ok_dir(data_dir: Path) -> Path:
    return data_dir / "ok"


@pytest.fixture(scope="session")
def broken_dir(data_dir: Path) -> Path:
    return data_dir / "broken"


def failure(directory: Path) -> ScenarioValidationError:
    with pytest.raises(ScenarioValidationError) as excinfo:
        load_scenario(directory)
    return excinfo.value


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


class TestLoadingAValidScenario:
    def test_everything_is_parsed_and_cross_linked(self, ok_dir: Path) -> None:
        loaded = load_scenario(ok_dir / "demo-scenario")
        assert loaded.id == "demo-scenario"
        assert loaded.scenario.version == 1
        assert "search_flights" in loaded.tool_names
        assert loaded.tool("search_flights") is not None
        assert loaded.tool("nope") is None
        assert set(loaded.fixtures) == {
            "fixtures/search_flights.json",
            "fixtures/get_fare_rules.json",
        }
        assert loaded.directory == ok_dir / "demo-scenario"
        assert len(loaded.config_hash) == 64

    def test_the_clock_keeps_its_offset(self, ok_dir: Path) -> None:
        loaded = load_scenario(ok_dir / "demo-scenario")
        assert loaded.scenario.clock.tzinfo is not None
        assert loaded.scenario.clock.utcoffset() is not None

    def test_the_mock_block_never_reaches_the_model(self, ok_dir: Path) -> None:
        loaded = load_scenario(ok_dir / "demo-scenario")
        payloads = loaded.openai_tools()
        assert len(payloads) == len(loaded.tools)
        assert all("mock" not in payload["function"] for payload in payloads)
        assert all(payload["type"] == "function" for payload in payloads)

    def test_fixture_paths_are_keyed_as_written(self, ok_dir: Path) -> None:
        loaded = load_scenario(ok_dir / "demo-scenario")
        for tool in loaded.tools:
            if isinstance(tool.mock, FixtureMock):
                assert tool.mock.file in loaded.fixtures

    def test_the_snapshot_is_json_ready(self, ok_dir: Path) -> None:
        snapshot = load_scenario(ok_dir / "demo-scenario").snapshot()
        assert snapshot["scenario"]["id"] == "demo-scenario"
        assert snapshot["config_hash"]
        assert isinstance(snapshot["tools"], list)

    def test_discovery_and_bulk_loading(self, ok_dir: Path) -> None:
        assert discover_scenario_dirs(ok_dir) == [ok_dir / "demo-scenario"]
        assert set(load_all_scenarios(ok_dir)) == {"demo-scenario"}

    def test_transcripts_are_optional(self, ok_dir: Path) -> None:
        assert load_transcripts(ok_dir / "demo-scenario") == {}

    def test_the_config_hash_follows_the_content(self, ok_dir: Path, tmp_path: Path) -> None:
        source = ok_dir / "demo-scenario"
        before = compute_config_hash(source)
        copy = tmp_path / "demo-scenario"
        copy.mkdir()
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            target = copy / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        assert compute_config_hash(copy) == before

        (copy / "seed.sql").write_text("CREATE TABLE t (a TEXT);\n", encoding="utf-8")
        assert compute_config_hash(copy) != before


# --------------------------------------------------------------------------- #
# deliberately broken scenarios
# --------------------------------------------------------------------------- #


class TestBrokenScenarios:
    def test_unknown_key(self, broken_dir: Path) -> None:
        error = failure(broken_dir / "unknown-key")
        assert error.file.endswith("scenario.yaml")
        assert error.path == "retries"
        assert "not permitted" in error.reason
        assert "unknown key" in str(error)

    def test_missing_fixture_file(self, broken_dir: Path) -> None:
        error = failure(broken_dir / "missing-file")
        assert error.file.endswith("search_flights.json")
        assert error.reason == "file not found"

    def test_invalid_json_schema(self, broken_dir: Path) -> None:
        error = failure(broken_dir / "bad-schema")
        assert error.file.endswith("tools.json")
        assert error.path == "[0].parameters"
        assert "invalid JSON Schema" in error.reason
        assert "search_flights" in error.reason

    def test_id_must_equal_the_directory_name(self, broken_dir: Path) -> None:
        error = failure(broken_dir / "id-mismatch")
        assert error.file.endswith("scenario.yaml")
        assert error.path == "id"
        assert "some-other-id" in error.reason
        assert "id-mismatch" in error.reason

    def test_duplicate_assertion_id(self, broken_dir: Path) -> None:
        error = failure(broken_dir / "duplicate-assertion")
        assert error.file.endswith("scenario.yaml")
        assert "duplicate assertion id 'searched'" in error.reason

    def test_reference_to_an_undefined_tool(self, broken_dir: Path) -> None:
        error = failure(broken_dir / "undefined-tool")
        assert error.file.endswith("scenario.yaml")
        assert error.path == "turns[0].assertions[booked].tool"
        assert "book_flight" in error.reason
        assert error.hint is not None
        assert "search_flights" in error.hint

    def test_every_broken_scenario_names_its_file(self, broken_dir: Path) -> None:
        directories = sorted(p for p in broken_dir.iterdir() if p.is_dir())
        assert len(directories) == 6
        for directory in directories:
            error = failure(directory)
            assert error.file, f"{directory.name} did not name a file"
            assert error.reason, f"{directory.name} did not give a reason"


class TestMissingInputs:
    def test_missing_scenario_directory(self, tmp_path: Path) -> None:
        error = failure(tmp_path / "ghost")
        assert error.reason == "scenario directory not found"

    def test_missing_scenario_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "empty-scenario").mkdir()
        error = failure(tmp_path / "empty-scenario")
        assert error.file.endswith("scenario.yaml")
        assert error.reason == "file not found"

    def test_scenarios_root_must_exist(self, tmp_path: Path) -> None:
        with pytest.raises(ScenarioValidationError, match="scenarios root not found"):
            discover_scenario_dirs(tmp_path / "ghost")


# --------------------------------------------------------------------------- #
# malformed files, built on the fly
# --------------------------------------------------------------------------- #


SCENARIO_YAML = """id: {sid}
version: 1
title: Built for one test
clock: "2026-03-02T09:00:00+04:00"
system_prompt: |
  You are a travel assistant.
{extra}turns:
  - user: "Find a flight."
    assertions:
      - id: searched
        type: tool_called
        axis: selection
        tool: search_flights
"""

TOOL_ENTRY = """
  {
    "name": "search_flights",
    "description": "Search available flights.",
    "parameters": {"type": "object", "properties": {"origin": {"type": "string"}}},
    "mock": {"kind": "fixture", "file": "fixtures/search_flights.json"}
  }
"""

TOOLS_JSON = f"[{TOOL_ENTRY}]"

FIXTURE_JSON = """{"responses": [], "default": {"error": "not_found"}}"""


def build(root: Path, name: str, **overrides: str) -> Path:
    """Write a minimal scenario directory, replacing named files wholesale."""
    directory = root / name
    (directory / "fixtures").mkdir(parents=True, exist_ok=True)
    files = {
        "scenario.yaml": SCENARIO_YAML.format(sid=name, extra=""),
        "tools.json": TOOLS_JSON,
        "fixtures/search_flights.json": FIXTURE_JSON,
    }
    files.update(overrides)
    for relative, text in files.items():
        (directory / relative).write_text(text, encoding="utf-8")
    return directory


class TestMalformedFiles:
    def test_invalid_yaml(self, tmp_path: Path) -> None:
        error = failure(build(tmp_path, "bad-yaml", **{"scenario.yaml": "id: [unclosed"}))
        assert error.file.endswith("scenario.yaml")
        assert "invalid YAML" in error.reason

    def test_scenario_yaml_must_be_a_mapping(self, tmp_path: Path) -> None:
        sequence = """- one
- two
"""
        error = failure(build(tmp_path, "not-a-mapping", **{"scenario.yaml": sequence}))
        assert "expected a mapping at the top level" in error.reason
        assert "list" in error.reason

    def test_invalid_json_points_at_the_line(self, tmp_path: Path) -> None:
        error = failure(build(tmp_path, "bad-json", **{"tools.json": "[{,}]"}))
        assert error.file.endswith("tools.json")
        assert "invalid JSON" in error.reason
        assert error.path is not None
        assert error.path.startswith("line ")

    def test_tools_json_must_be_a_list(self, tmp_path: Path) -> None:
        error = failure(build(tmp_path, "tools-not-a-list", **{"tools.json": "{}"}))
        assert "expected a list of tool definitions" in error.reason

    def test_tools_json_must_not_be_empty(self, tmp_path: Path) -> None:
        error = failure(build(tmp_path, "no-tools", **{"tools.json": "[]"}))
        assert "at least one tool" in error.reason

    def test_a_malformed_tool_entry_names_its_index(self, tmp_path: Path) -> None:
        broken = """[{"name": "search_flights", "description": "x"}]"""
        error = failure(build(tmp_path, "bad-tool", **{"tools.json": broken}))
        assert error.file.endswith("tools.json")
        assert error.path == "[0].parameters"

    def test_duplicate_tool_names(self, tmp_path: Path) -> None:
        doubled = f"[{TOOL_ENTRY},{TOOL_ENTRY}]"
        error = failure(build(tmp_path, "dupe-tool", **{"tools.json": doubled}))
        assert "duplicate tool name" in error.reason
        assert error.path == "[1].name"

    def test_a_malformed_fixture_file(self, tmp_path: Path) -> None:
        rows = """{"responses": [{"match": {}}]}"""
        error = failure(build(tmp_path, "bad-fixture", **{"fixtures/search_flights.json": rows}))
        assert error.file.endswith("search_flights.json")
        assert error.path == "responses.0.response"

    def test_a_fault_naming_an_unknown_tool(self, tmp_path: Path) -> None:
        faults = """faults:
  - tool: cancel_booking
    on_call: 1
    response: {error: "QueryTimeout"}
"""
        yaml_text = SCENARIO_YAML.format(sid="bad-fault", extra=faults)
        error = failure(build(tmp_path, "bad-fault", **{"scenario.yaml": yaml_text}))
        assert error.file.endswith("scenario.yaml")
        assert error.path == "faults[0].tool"
        assert "cancel_booking" in error.reason
