"""FakeModel: replays a scripted transcript instead of calling a provider (SPEC 6.4).

Transcripts are the test suite for scenarios, so this provider is deliberately
strict: it never improvises. If the conversation runs past what the transcript
scripts, that is a bug in the transcript (or in the loop) and it is raised, not
papered over.
"""

from __future__ import annotations

import json
from typing import Any

from evalharness.runner.provider import ProviderError
from evalharness.schema.runtime import AssistantMessage, ToolCall
from evalharness.schema.transcript import ScriptedStep, Transcript


class TranscriptExhaustedError(ProviderError):
    """The runner asked for a step the transcript does not script."""


class FakeModel:
    """Replays ``transcript`` as if it were a model.

    The position in the script is derived from the message list rather than from
    internal counters, so the fake stays honest even if the runner retries or
    rebuilds the conversation: the turn is the number of user messages so far,
    and the step is the number of assistant messages since the last one.
    """

    name = "fake"

    def __init__(self, transcript: Transcript, *, label: str = "transcript") -> None:
        self.transcript = transcript
        self.label = label
        self.calls: int = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        call_timeout_s: float | None = None,
        **params: Any,
    ) -> AssistantMessage:
        turn_index, step_index = self._position(messages)
        step = self._step(turn_index, step_index)
        self.calls += 1
        return AssistantMessage(
            content=step.content,
            tool_calls=[
                ToolCall(
                    id=f"call_{turn_index}_{step_index}_{i}",
                    name=scripted.tool,
                    arguments=dict(scripted.args),
                    raw_arguments=json.dumps(scripted.args, sort_keys=True),
                )
                for i, scripted in enumerate(step.tool_calls)
            ],
            finish_reason="tool_calls" if step.tool_calls else "stop",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            latency_ms=0,
        )

    @staticmethod
    def _position(messages: list[dict[str, Any]]) -> tuple[int, int]:
        turn_index = sum(1 for m in messages if m.get("role") == "user") - 1
        step_index = 0
        for message in reversed(messages):
            role = message.get("role")
            if role == "user":
                break
            if role == "assistant":
                step_index += 1
        return turn_index, step_index

    def _step(self, turn_index: int, step_index: int) -> ScriptedStep:
        where = f"{self.label} (scenario {self.transcript.scenario!r})"
        if turn_index < 0:
            raise TranscriptExhaustedError(f"{where}: asked for a completion with no user message")
        if turn_index >= len(self.transcript.turns):
            raise TranscriptExhaustedError(
                f"{where}: scripts {len(self.transcript.turns)} turn(s) "
                f"but the runner reached turn index {turn_index}"
            )
        steps = self.transcript.turns[turn_index].steps
        if step_index >= len(steps):
            raise TranscriptExhaustedError(
                f"{where}: turn {turn_index} scripts {len(steps)} step(s) "
                f"but the runner asked for step {step_index}; "
                "the last scripted step probably still requests tool calls"
            )
        return steps[step_index]
