"""Tests for the optional LLM-judge assertion (SPEC 4.3, Phase 8).

The judge is the one non-deterministic grader, so the properties worth pinning
are: it stays off unless explicitly enabled, its results are always flagged
non-deterministic, and a flaky judge degrades to a failed assertion with a
readable reason rather than taking the attempt down with it.

The "judge model" here is a scripted stub. No network, as everywhere else.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from evalharness.config import get_settings
from evalharness.grader.judge import (
    grade_judge_assertions,
    judge_assertions,
    judge_enabled,
    render_attempt,
    render_turn,
)
from evalharness.loader.scenario_loader import LoadedScenario
from evalharness.schema.enums import Axis, Severity
from evalharness.schema.runtime import (
    AssistantMessage,
    AttemptResult,
    ToolCall,
    ToolCallRecord,
    ToolResult,
    TurnRecord,
)
from evalharness.schema.scenario import Scenario
from evalharness.schema.tools import ToolDefinition


class ScriptedJudge:
    """A provider that answers every call with one canned message."""

    name = "scripted-judge"

    def __init__(self, content: str | None = '{"passed": true, "reason": "looks right"}') -> None:
        self.content = content
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **params: Any
    ) -> AssistantMessage:
        self.calls.append(messages)
        return AssistantMessage(content=self.content)


class ExplodingJudge:
    name = "exploding-judge"

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **params: Any
    ) -> AssistantMessage:
        raise RuntimeError("judge endpoint is down")


def scenario_with_judge(scope: str = "turn") -> LoadedScenario:
    definition = Scenario.model_validate(
        {
            "id": "judged",
            "version": 1,
            "title": "Judged",
            "clock": "2026-03-02T09:00:00+04:00",
            "system_prompt": "be helpful",
            "turns": [
                {
                    "user": "help me",
                    "assertions": [
                        {
                            "id": "polite",
                            "type": "judge",
                            "axis": "clarification",
                            "severity": "soft",
                            "scope": scope,
                            "rubric": "Did the assistant explain why it declined?",
                        }
                    ],
                }
            ],
        }
    )
    tool = ToolDefinition.model_validate(
        {
            "name": "noop",
            "description": "does nothing",
            "parameters": {"type": "object", "properties": {}},
            "mock": {"kind": "fixture", "file": "fixtures/noop.json"},
        }
    )
    return LoadedScenario(
        scenario=definition,
        tools=[tool],
        fixtures={},
        expected={},
        directory=Path(),
        config_hash="deadbeef",
    )


def attempt_with_one_turn() -> AttemptResult:
    record = ToolCallRecord(
        seq=1,
        turn_index=0,
        step=0,
        batch_index=0,
        call=ToolCall(id="c1", name="noop", arguments={"x": 1}),
        result=ToolResult(ok=True, content={"done": True}),
    )
    return AttemptResult(
        scenario_id="judged",
        repetition=0,
        turns=[
            TurnRecord(
                index=0,
                user_message="help me",
                final_response="I cannot do that, because the policy forbids it.",
                tool_calls=[record],
            )
        ],
    )


@pytest.fixture
def judging_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("EVALHARNESS_ENABLE_JUDGE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# Off by default                                                               #
# --------------------------------------------------------------------------- #


def test_judging_is_disabled_by_default() -> None:
    assert judge_enabled() is False


async def test_nothing_is_graded_while_judging_is_disabled() -> None:
    """The default configuration must not make a model call, ever."""
    judge = ScriptedJudge()
    results = await grade_judge_assertions(scenario_with_judge(), attempt_with_one_turn(), judge)
    assert results == []
    assert judge.calls == []


async def test_nothing_is_graded_without_a_judge_provider(judging_enabled: None) -> None:
    results = await grade_judge_assertions(scenario_with_judge(), attempt_with_one_turn(), None)
    assert results == []


def test_judge_assertions_are_discovered_with_their_turn_index() -> None:
    found = judge_assertions(scenario_with_judge())
    assert [(index, assertion.id) for index, assertion in found] == [(0, "polite")]


# --------------------------------------------------------------------------- #
# Grading                                                                      #
# --------------------------------------------------------------------------- #


async def test_a_passing_verdict_is_recorded_and_flagged(judging_enabled: None) -> None:
    judge = ScriptedJudge('{"passed": true, "reason": "It gave a clear policy reason."}')
    (result,) = await grade_judge_assertions(scenario_with_judge(), attempt_with_one_turn(), judge)

    assert result.assertion_id == "polite"
    assert result.passed is True
    assert result.reason == "It gave a clear policy reason."
    assert result.axis is Axis.CLARIFICATION
    assert result.severity is Severity.SOFT
    assert result.turn_index == 0
    # SPEC 4.3: judge results are always separable from deterministic ones.
    assert result.non_deterministic is True
    assert str(result.details["rubric"]).startswith("Did the assistant")


async def test_a_failing_verdict_is_recorded(judging_enabled: None) -> None:
    judge = ScriptedJudge('{"passed": false, "reason": "It just refused."}')
    (result,) = await grade_judge_assertions(scenario_with_judge(), attempt_with_one_turn(), judge)
    assert result.passed is False
    assert result.reason == "It just refused."


async def test_the_rubric_and_transcript_reach_the_judge(judging_enabled: None) -> None:
    judge = ScriptedJudge()
    await grade_judge_assertions(scenario_with_judge(), attempt_with_one_turn(), judge)

    system, user = judge.calls[0]
    assert system["role"] == "system"
    assert "JSON object" in system["content"]
    assert "Did the assistant explain why it declined?" in user["content"]
    assert "policy forbids it" in user["content"]


async def test_scenario_scope_judges_the_whole_attempt(judging_enabled: None) -> None:
    judge = ScriptedJudge()
    (result,) = await grade_judge_assertions(
        scenario_with_judge(scope="scenario"), attempt_with_one_turn(), judge
    )
    # Scenario scope is not tied to a turn.
    assert result.turn_index is None


async def test_a_turn_the_attempt_never_reached_is_skipped(judging_enabled: None) -> None:
    """An attempt cut short by a critical failure has no turn to judge."""
    empty = AttemptResult(scenario_id="judged", repetition=0, turns=[])
    results = await grade_judge_assertions(scenario_with_judge(), empty, ScriptedJudge())
    assert results == []


# --------------------------------------------------------------------------- #
# Bad judge replies degrade, never crash                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reply", "expected_reason"),
    [
        (None, "empty response"),
        ("", "empty response"),
        ("I think it was fine, honestly.", "did not return JSON"),
        ('{"passed": true, ', "did not return JSON"),
        ('{"passed": tru}', "invalid JSON"),
        ('{"verdict": "good"}', "no 'passed' field"),
    ],
)
async def test_unparseable_verdicts_fail_with_a_readable_reason(
    judging_enabled: None, reply: str | None, expected_reason: str
) -> None:
    (result,) = await grade_judge_assertions(
        scenario_with_judge(), attempt_with_one_turn(), ScriptedJudge(reply)
    )
    assert result.passed is False
    assert expected_reason in result.reason
    assert result.non_deterministic is True


async def test_json_embedded_in_prose_is_still_read(judging_enabled: None) -> None:
    """Judges commonly wrap the object in commentary or a code fence."""
    judge = ScriptedJudge('Here is my verdict:\n```json\n{"passed": true, "reason": "ok"}\n```')
    (result,) = await grade_judge_assertions(scenario_with_judge(), attempt_with_one_turn(), judge)
    assert result.passed is True
    assert result.reason == "ok"


async def test_a_verdict_without_a_reason_still_says_something(judging_enabled: None) -> None:
    (result,) = await grade_judge_assertions(
        scenario_with_judge(), attempt_with_one_turn(), ScriptedJudge('{"passed": true}')
    )
    assert result.passed is True
    assert result.reason == "(judge gave no reason)"


async def test_a_failing_judge_call_does_not_take_down_the_attempt(
    judging_enabled: None,
) -> None:
    (result,) = await grade_judge_assertions(
        scenario_with_judge(), attempt_with_one_turn(), ExplodingJudge()
    )
    assert result.passed is False
    assert "judge call failed" in result.reason
    assert "judge endpoint is down" in result.reason


# --------------------------------------------------------------------------- #
# Transcript rendering                                                         #
# --------------------------------------------------------------------------- #


def test_render_turn_shows_calls_and_outcomes() -> None:
    rendered = render_turn(attempt_with_one_turn().turns[0])
    assert "USER: help me" in rendered
    assert 'TOOL CALL: noop({"x": 1}) -> ok' in rendered
    assert "ASSISTANT: I cannot do that" in rendered


def test_render_turn_marks_failed_calls() -> None:
    turn = attempt_with_one_turn().turns[0]
    turn.tool_calls[0].result = ToolResult(ok=False, error="not_found")
    assert "-> error: not_found" in render_turn(turn)


def test_render_turn_handles_a_turn_with_no_final_message() -> None:
    turn = attempt_with_one_turn().turns[0]
    turn.final_response = None
    assert "<no final message>" in render_turn(turn)


def test_render_attempt_joins_every_turn() -> None:
    result = attempt_with_one_turn()
    result.turns.append(TurnRecord(index=1, user_message="and again", final_response="done"))
    rendered = render_attempt(result)
    assert "USER: help me" in rendered
    assert "USER: and again" in rendered
