"""Truncation is recorded as its own event (DECISIONS D35).

A response that stops on ``finish_reason=length`` was cut off by ``max_tokens``
mid-thought. Nothing the harness enforces stopped it, so without a distinct
event it grades exactly like a model that chose to stop -- a missing tool call
reads as a decision rather than an interruption. The attempt is still graded,
because it did produce a verdict, but the verdict is flagged for review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evalharness.loader import load_scenario, load_transcripts
from evalharness.runner import FakeModel
from evalharness.runner.conversation import AttemptContext, run_attempt
from evalharness.schema.enums import EventType
from evalharness.schema.runtime import AssistantMessage

TRAVEL_BOOKING = Path(__file__).resolve().parents[2] / "scenarios" / "travel-booking"


class TruncatingModel(FakeModel):
    """Replays the golden transcript but reports every stop as a cut-off."""

    def __init__(self, transcript: Any, *, reasoning: int | None = 900) -> None:
        super().__init__(transcript)
        self.reasoning = reasoning

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        call_timeout_s: float | None = None,
        **params: Any,
    ) -> AssistantMessage:
        message = await super().complete(messages, tools, **params)
        message.finish_reason = "length"
        message.output_tokens = 16384
        message.reasoning_tokens = self.reasoning
        return message


async def run_with(provider: FakeModel) -> Any:
    loaded = load_scenario(TRAVEL_BOOKING)
    return await run_attempt(AttemptContext(loaded=loaded, provider=provider, repetition=0))


def truncations(result: Any) -> list[Any]:
    return [e for e in result.events if e.type is EventType.TRUNCATED]


# --------------------------------------------------------------------------- #
# The event                                                                    #
# --------------------------------------------------------------------------- #


async def test_a_length_finish_records_a_truncated_event() -> None:
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_with(TruncatingModel(golden))

    assert truncations(result), "finish_reason=length must leave a trace"
    assert result.truncated is True


async def test_a_normal_finish_records_nothing() -> None:
    """The flag must mean something: a clean run carries no truncation."""
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_with(FakeModel(golden))

    assert truncations(result) == []
    assert result.truncated is False


async def test_the_event_carries_the_token_counts() -> None:
    """Sizing a max_tokens budget needs the numbers, not just the fact."""
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_with(TruncatingModel(golden, reasoning=1234))

    payload = truncations(result)[0].payload
    assert payload["output_tokens"] == 16384
    assert payload["reasoning_tokens"] == 1234
    assert "finish_reason=length" in str(payload["detail"])


async def test_unreported_reasoning_stays_none() -> None:
    """None means 'the provider did not say', which is not the same as zero."""
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_with(TruncatingModel(golden, reasoning=None))

    assert truncations(result)[0].payload["reasoning_tokens"] is None


async def test_a_truncated_attempt_is_still_graded() -> None:
    """It produced a verdict, so it belongs in the rates -- flagged, not dropped.

    Excluding it would quietly shrink coverage; counting it silently would let a
    cut-off response masquerade as a considered one. It is counted and marked.
    """
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_with(TruncatingModel(golden))

    assert result.graded is True
    assert result.errored is False


# --------------------------------------------------------------------------- #
# Per-call tokens (SPEC 8.3)                                                   #
# --------------------------------------------------------------------------- #


async def test_every_model_response_carries_its_own_token_counts() -> None:
    """Attempt totals cannot size a per-call budget; per-call numbers can."""
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]
    result = await run_with(TruncatingModel(golden, reasoning=77))

    responses = [e for e in result.events if e.type is EventType.MODEL_RESPONSE]
    assert responses
    for event in responses:
        assert event.output_tokens == 16384, "the column must be populated"
        assert event.payload["output_tokens"] == 16384, "and mirrored into the payload"
        assert event.payload["reasoning_tokens"] == 77
        assert "input_tokens" in event.payload
