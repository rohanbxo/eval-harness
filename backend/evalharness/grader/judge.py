"""LLM-judge assertions (SPEC 4.3) -- off by default.

Everything else in the grader is deterministic; this is not. Judge results are
always flagged ``non_deterministic`` so the UI and reports can separate them
from the assertions that would give the same verdict twice in a row.

Nothing calls into here unless ``EVALHARNESS_ENABLE_JUDGE`` is true *and* a
judge provider is supplied.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from evalharness.config import get_settings
from evalharness.loader.scenario_loader import LoadedScenario
from evalharness.schema.assertions import JudgeAssertion
from evalharness.schema.enums import Scope
from evalharness.schema.runtime import AssertionResult, AttemptResult, TurnRecord

if TYPE_CHECKING:
    # Import-time only: `evalharness.runner` imports the conversation loop, which
    # imports this module. Provider is used here purely as an annotation, so
    # deferring it keeps `import evalharness.grader.judge` working on its own.
    from evalharness.runner.provider import Provider

JUDGE_SYSTEM_PROMPT = (
    "You are grading an AI assistant's behavior against a rubric. "
    "Read the transcript, then answer with a single JSON object: "
    '{"passed": true|false, "reason": "<one or two sentences>"}. '
    "Output nothing else."
)


def judge_assertions(loaded: LoadedScenario) -> list[tuple[int, JudgeAssertion]]:
    """Every judge assertion in the scenario, paired with its turn index."""
    return [
        (index, assertion)
        for index, turn in enumerate(loaded.scenario.turns)
        for assertion in turn.assertions
        if isinstance(assertion, JudgeAssertion)
    ]


def judge_enabled() -> bool:
    return get_settings().enable_judge


def render_turn(turn: TurnRecord) -> str:
    """A plain-text rendering of one turn for the judge to read."""
    lines = [f"USER: {turn.user_message}"]
    for record in turn.tool_calls:
        args = json.dumps(record.arguments, sort_keys=True, default=str)
        status = "ok" if record.result.ok else f"error: {record.result.error}"
        lines.append(f"TOOL CALL: {record.name}({args}) -> {status}")
    lines.append(f"ASSISTANT: {turn.final_response or '<no final message>'}")
    return "\n".join(lines)


def render_attempt(result: AttemptResult) -> str:
    return "\n\n".join(render_turn(turn) for turn in result.turns)


def _parse_verdict(content: str | None) -> tuple[bool, str]:
    """Pull ``{"passed": ..., "reason": ...}`` out of the judge's reply."""
    if not content:
        return False, "judge returned an empty response"
    found = re.search(r"\{.*\}", content, re.DOTALL)
    if found is None:
        return False, f"judge did not return JSON: {content.strip()[:200]}"
    try:
        payload: Any = json.loads(found.group(0))
    except ValueError as exc:
        return False, f"judge returned invalid JSON ({exc}): {found.group(0)[:200]}"
    if not isinstance(payload, dict) or "passed" not in payload:
        return False, f"judge response has no 'passed' field: {found.group(0)[:200]}"
    reason = payload.get("reason")
    return bool(payload["passed"]), str(reason) if reason else "(judge gave no reason)"


async def grade_judge(
    assertion: JudgeAssertion,
    transcript: str,
    provider: Provider,
    *,
    turn_index: int | None,
) -> AssertionResult:
    """Ask the judge model to apply one rubric. Always non-deterministic."""
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"RUBRIC:\n{assertion.rubric}\n\nTRANSCRIPT:\n{transcript}",
        },
    ]
    try:
        reply = await provider.complete(messages, [])
        passed, reason = _parse_verdict(reply.content)
    except Exception as exc:
        passed, reason = False, f"judge call failed: {type(exc).__name__}: {exc}"
    return AssertionResult(
        assertion_id=assertion.id,
        type=assertion.type,
        axis=assertion.axis,
        severity=assertion.severity,
        turn_index=turn_index,
        passed=passed,
        reason=reason,
        details={"rubric": assertion.rubric},
        non_deterministic=True,
    )


async def grade_judge_assertions(
    loaded: LoadedScenario, result: AttemptResult, provider: Provider | None
) -> list[AssertionResult]:
    """Grade every judge assertion, or nothing at all when judging is disabled."""
    if provider is None or not judge_enabled():
        return []
    out: list[AssertionResult] = []
    for turn_index, assertion in judge_assertions(loaded):
        if assertion.scope is Scope.SCENARIO:
            transcript = render_attempt(result)
            scoped_index: int | None = None
        else:
            turn = next((t for t in result.turns if t.index == turn_index), None)
            if turn is None:
                continue
            transcript = render_turn(turn)
            scoped_index = turn_index
        out.append(await grade_judge(assertion, transcript, provider, turn_index=scoped_index))
    return out
