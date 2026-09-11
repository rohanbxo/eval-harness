"""Mock tool engine tests (SPEC 5.1, 5.2, 5.4).

``execute`` never raises: a hallucinated tool, malformed arguments and a broken
handler all come back as ``ok=False`` results, because each of those is a graded
model behavior rather than a harness crash.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from evalharness.engine import ToolEngine, register_handler, select_fixture
from evalharness.engine.faults import fault_result, select_fault
from evalharness.engine.fixtures import fixture_result
from evalharness.engine.handlers import SCENARIO_DIR_KEY
from evalharness.loader import LoadedScenario, load_scenario
from evalharness.schema.runtime import ToolResult
from evalharness.schema.scenario import Fault
from evalharness.schema.tools import FixtureFile, HandlerMock, ToolDefinition

RIYADH = {"origin": "DXB", "destination": "RUH", "date": "2026-03-06"}


@pytest.fixture(scope="session")
def demo_dir(data_dir: Path) -> Path:
    return data_dir / "ok" / "demo-scenario"


@pytest.fixture(scope="session")
def demo(demo_dir: Path) -> LoadedScenario:
    return load_scenario(demo_dir)


@pytest.fixture
def engine(demo: LoadedScenario) -> Iterator[ToolEngine]:
    built = ToolEngine(demo)
    yield built
    built.reset()


# --------------------------------------------------------------------------- #
# fixture dispatch (SPEC 5.2)
# --------------------------------------------------------------------------- #


class TestFixtureDispatch:
    def test_first_matching_response_wins(self, engine: ToolEngine) -> None:
        outcome = engine.execute("search_flights", RIYADH)
        assert outcome.result.ok
        assert outcome.result.error is None
        assert outcome.result.content == {
            "flights": [
                {"id": "FL-101", "price_aed": 640, "refundable": False},
                {"id": "FL-204", "price_aed": 910, "refundable": True},
            ]
        }

    def test_matchers_apply_to_the_call_arguments(self, engine: ToolEngine) -> None:
        """The fixture's ``ci`` matchers accept the city names, not just the codes."""
        outcome = engine.execute(
            "search_flights", {"origin": "dubai", "destination": "riyadh", "date": "2026-03-06"}
        )
        assert outcome.result.ok

    def test_a_row_can_declare_itself_an_error(self, engine: ToolEngine) -> None:
        outcome = engine.execute("search_flights", {**RIYADH, "destination": "JED"})
        assert not outcome.result.ok
        assert outcome.result.error == "no_availability"
        assert not outcome.faulted

    def test_default_answers_unmatched_calls(self, engine: ToolEngine) -> None:
        outcome = engine.execute("search_flights", {**RIYADH, "destination": "XXX"})
        assert not outcome.result.ok
        assert outcome.result.error == "unsupported_route"
        assert outcome.result.content == {
            "error": "unsupported_route",
            "message": "That route is not served.",
        }

    def test_default_is_an_error_by_default(self, engine: ToolEngine) -> None:
        outcome = engine.execute("get_fare_rules", {"flight_id": "FL-999"})
        assert not outcome.result.ok
        assert outcome.result.error == "not_found"

    def test_responses_are_returned_verbatim(self, engine: ToolEngine) -> None:
        outcome = engine.execute("get_fare_rules", {"flight_id": "FL-204"})
        assert outcome.result.content == {"refundable": True, "change_fee_aed": 0}

    def test_selection_reports_which_row_answered(self, demo: LoadedScenario) -> None:
        fixture = demo.fixtures["fixtures/get_fare_rules.json"]
        assert select_fixture(fixture, {"flight_id": "FL-204"}).index == 1
        assert select_fixture(fixture, {"flight_id": "FL-101"}).index == 0
        fallback = select_fixture(fixture, {"flight_id": "nope"})
        assert fallback.is_default and fallback.index is None

    def test_error_label_falls_back_when_the_payload_has_no_error_key(self) -> None:
        fixture = FixtureFile.model_validate(
            {
                "responses": [{"match": {}, "response": {"status": "down"}, "ok": False}],
                "default": "nothing here",
            }
        )
        assert fixture_result(fixture, {}).error == "tool_error"
        empty = FixtureFile.model_validate({"responses": [], "default": {"error": ""}})
        assert fixture_result(empty, {}).error == "no_matching_fixture"

    def test_a_default_can_be_declared_successful(self) -> None:
        fixture = FixtureFile.model_validate({"responses": [], "default": [], "default_ok": True})
        result = fixture_result(fixture, {})
        assert result.ok and result.content == []


# --------------------------------------------------------------------------- #
# unknown tools and invalid arguments (SPEC 5.1)
# --------------------------------------------------------------------------- #


class TestUnknownToolAndBadArguments:
    def test_unknown_tool_never_raises(self, engine: ToolEngine) -> None:
        outcome = engine.execute("teleport", {"to": "mars"})
        assert not outcome.result.ok
        assert outcome.result.error == "unknown_tool"
        assert isinstance(outcome.result.content, dict)
        assert outcome.result.content["error"] == "unknown_tool"
        assert "search_flights" in str(outcome.result.content["message"])

    def test_unknown_tool_does_not_touch_the_call_counters(self, engine: ToolEngine) -> None:
        engine.execute("teleport", {})
        assert engine.call_counts == {}

    def test_missing_required_argument(self, engine: ToolEngine) -> None:
        outcome = engine.execute("search_flights", {"origin": "DXB"})
        assert not outcome.result.ok
        assert outcome.result.error == "invalid_arguments"
        assert isinstance(outcome.result.content, dict)
        details = outcome.result.content["details"]
        assert isinstance(details, list)
        messages = [d["message"] for d in details if isinstance(d, dict)]
        assert "'destination' is a required property" in messages
        assert "'date' is a required property" in messages

    def test_wrong_type_and_bad_enum(self, engine: ToolEngine) -> None:
        outcome = engine.execute("search_flights", {**RIYADH, "adults": "two"})
        assert outcome.result.error == "invalid_arguments"
        assert isinstance(outcome.result.content, dict)
        details = outcome.result.content["details"]
        assert isinstance(details, list)
        first = details[0]
        assert isinstance(first, dict)
        assert first["path"] == "adults"

        enum_outcome = engine.execute("search_flights", {**RIYADH, "cabin": "first"})
        assert enum_outcome.result.error == "invalid_arguments"

    def test_additional_properties_are_rejected(self, engine: ToolEngine) -> None:
        outcome = engine.execute("search_flights", {**RIYADH, "seat": "12A"})
        assert outcome.result.error == "invalid_arguments"

    def test_invalid_arguments_still_count_as_a_call(self, engine: ToolEngine) -> None:
        engine.execute("search_flights", {"origin": "DXB"})
        assert engine.calls_to("search_flights") == 1


# --------------------------------------------------------------------------- #
# faults (SPEC 5.4)
# --------------------------------------------------------------------------- #


class TestFaults:
    def test_fault_fires_on_the_scripted_call_index(self, engine: ToolEngine) -> None:
        first = engine.execute("search_flights", RIYADH)
        assert first.result.ok and not first.faulted

        second = engine.execute("search_flights", RIYADH)
        assert second.faulted
        assert second.fault is not None
        assert second.fault.on_call == 2
        assert not second.result.ok
        assert second.result.error == "SearchTimeout"
        assert second.result.content == {
            "error": "SearchTimeout",
            "message": "Search exceeded 5s. Retry.",
        }

        third = engine.execute("search_flights", RIYADH)
        assert not third.faulted
        assert third.result.ok

    def test_counters_are_per_tool(self, engine: ToolEngine) -> None:
        engine.execute("get_fare_rules", {"flight_id": "FL-204"})
        engine.execute("get_fare_rules", {"flight_id": "FL-204"})
        outcome = engine.execute("search_flights", RIYADH)
        assert not outcome.faulted, "get_fare_rules calls must not advance search_flights"

    def test_reset_rewinds_the_counter_between_attempts(self, engine: ToolEngine) -> None:
        engine.execute("search_flights", RIYADH)
        engine.execute("search_flights", RIYADH)
        engine.reset()
        assert engine.call_counts == {}
        assert not engine.execute("search_flights", RIYADH).faulted

    def test_a_faulted_call_still_advances_the_counter(self, engine: ToolEngine) -> None:
        engine.execute("search_flights", RIYADH)
        engine.execute("search_flights", RIYADH)
        assert engine.calls_to("search_flights") == 2

    def test_faults_beat_dispatch_but_not_argument_validation(self, engine: ToolEngine) -> None:
        engine.execute("search_flights", {"origin": "DXB"})  # rejected, counts as call 1
        outcome = engine.execute("search_flights", RIYADH)  # call 2 -> faulted
        assert outcome.faulted

    def test_select_fault_is_exact_on_tool_and_index(self) -> None:
        faults = [
            Fault(tool="run_sql", on_call=1, response={"error": "QueryTimeout"}),
            Fault(tool="run_sql", on_call=3, response={"error": "Again"}),
        ]
        assert select_fault(faults, "run_sql", 1) is faults[0]
        assert select_fault(faults, "run_sql", 2) is None
        assert select_fault(faults, "run_sql", 3) is faults[1]
        assert select_fault(faults, "other", 1) is None

    def test_a_fault_can_be_declared_successful(self) -> None:
        fault = Fault(tool="run_sql", on_call=1, response={"rows": []}, ok=True)
        assert fault_result(fault).ok

    def test_fault_without_an_error_key_gets_a_generic_label(self) -> None:
        fault = Fault(tool="run_sql", on_call=1, response="boom")
        result = fault_result(fault)
        assert not result.ok and result.error == "injected_fault"


# --------------------------------------------------------------------------- #
# handler dispatch (SPEC 5.3)
# --------------------------------------------------------------------------- #


class TestHandlerDispatch:
    def test_handler_tools_run_through_the_engine(self, engine: ToolEngine) -> None:
        outcome = engine.execute("run_sql", {"sql": "SELECT code FROM airports ORDER BY code"})
        assert outcome.result.ok
        assert isinstance(outcome.result.content, dict)
        assert outcome.result.content["rows"] == [
            {"code": "DXB"},
            {"code": "JED"},
            {"code": "RUH"},
        ]

    def test_read_only_enforcement_reaches_the_model_as_an_error(self, engine: ToolEngine) -> None:
        outcome = engine.execute("run_sql", {"sql": "SELECT * FROM bookings; DROP TABLE bookings"})
        assert not outcome.result.ok
        assert outcome.result.error == "read_only_violation"
        # ... and the table is still there.
        after = engine.execute("run_sql", {"sql": "SELECT COUNT(*) AS n FROM bookings"})
        assert after.result.ok
        assert isinstance(after.result.content, dict)
        assert after.result.content["rows"] == [{"n": 2}]

    def test_state_is_shared_across_calls_and_cleared_by_reset(self, engine: ToolEngine) -> None:
        engine.execute("list_tables", {})
        assert engine.state, "the handler should have cached its connection"
        engine.reset()
        assert engine.state == {}

    def test_an_unregistered_handler_name_is_an_error_not_a_crash(self, engine: ToolEngine) -> None:
        outcome = engine.execute("no_such_handler", {})
        assert outcome.result.error == "unknown_handler"
        assert isinstance(outcome.result.content, dict)
        assert "sqlite_query" in str(outcome.result.content["message"])

    def test_a_handler_that_raises_is_contained(self, demo: LoadedScenario) -> None:
        def boom(
            args: Mapping[str, JsonValue],
            state: dict[str, Any],
            config: Mapping[str, JsonValue],
        ) -> ToolResult:
            raise RuntimeError("handler went wrong")

        register_handler("test_boom", boom)
        tool = ToolDefinition(
            name="boom_tool",
            description="Always raises.",
            parameters={"type": "object", "properties": {}},
            mock=HandlerMock(name="test_boom"),
        )
        engine = ToolEngine(replace(demo, tools=[*demo.tools, tool]))
        outcome = engine.execute("boom_tool", {})
        assert not outcome.result.ok
        assert outcome.result.error == "handler_error"
        assert isinstance(outcome.result.content, dict)
        assert "RuntimeError" in str(outcome.result.content["message"])

    def test_the_scenario_directory_is_injected_into_the_handler_config(
        self, demo: LoadedScenario, demo_dir: Path
    ) -> None:
        seen: dict[str, JsonValue] = {}

        def spy(
            args: Mapping[str, JsonValue],
            state: dict[str, Any],
            config: Mapping[str, JsonValue],
        ) -> ToolResult:
            seen.update(config)
            return ToolResult(ok=True, content=None)

        register_handler("test_spy", spy)
        tool = ToolDefinition(
            name="spy_tool",
            description="Records its config.",
            parameters={"type": "object", "properties": {}},
            mock=HandlerMock(name="test_spy", config={"seed": "seed.sql"}),
        )
        engine = ToolEngine(replace(demo, tools=[*demo.tools, tool]))
        assert engine.execute("spy_tool", {}).result.ok
        assert seen["seed"] == "seed.sql"
        assert seen[SCENARIO_DIR_KEY] == str(demo_dir.resolve())


def test_tool_names_are_exposed_for_the_runner(engine: ToolEngine, demo: LoadedScenario) -> None:
    assert engine.tool_names == demo.tool_names
