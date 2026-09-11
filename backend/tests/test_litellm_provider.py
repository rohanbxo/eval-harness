"""Tests for the LiteLLM adapter (SPEC 6.4, 6.3).

This is the only code path that talks to a real provider in production, so it is
also the one that must never be exercised against one in a test. Every test here
injects `acompletion` and `sleep`, and an autouse fixture puts a stub `litellm`
in `sys.modules` so the real package is never imported -- which keeps the module
honest and takes it from ~8s to under half a second.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from evalharness.runner.litellm_provider import (
    MAX_RETRY_DELAY_S,
    LiteLLMProvider,
    is_retryable,
    parse_tool_call,
    retry_after_seconds,
)
from evalharness.runner.provider import ProviderError
from evalharness.schema.registry import ModelEntry, Pricing

# --------------------------------------------------------------------------- #
# Fakes shaped like the objects LiteLLM returns                                #
# --------------------------------------------------------------------------- #


@dataclass
class FakeFunction:
    name: str
    arguments: Any


@dataclass
class FakeToolCall:
    id: str
    function: FakeFunction


@dataclass
class FakeMessage:
    content: str | None = None
    tool_calls: list[FakeToolCall] = field(default_factory=list)


@dataclass
class FakeChoice:
    message: FakeMessage | None
    finish_reason: str | None = "stop"


@dataclass
class FakeUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class FakeResponse:
    choices: list[FakeChoice]
    usage: FakeUsage | None = None


def response(
    content: str | None = "ok",
    tool_calls: list[FakeToolCall] | None = None,
    *,
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
    finish_reason: str = "stop",
) -> FakeResponse:
    return FakeResponse(
        choices=[
            FakeChoice(
                message=FakeMessage(content=content, tool_calls=tool_calls or []),
                finish_reason=finish_reason,
            )
        ],
        usage=FakeUsage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class HttpError(Exception):
    """Stands in for a vendor error that carries a status code."""

    def __init__(self, status_code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(Exception):
    """A transient error known only by its class name, with no status code."""


class AuthenticationError(Exception):
    """A permanent error with no status code."""


@pytest.fixture(autouse=True)
def _no_real_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the real `litellm` out of the test process entirely.

    `cost_for` imports it lazily, and importing it for real costs seconds. Tests
    that care about cost install their own stub over this one.
    """
    stub = types.ModuleType("litellm")
    stub.completion_cost = lambda **_: None  # type: ignore[attr-defined]
    stub.drop_params = False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", stub)


def entry(**overrides: Any) -> ModelEntry:
    base: dict[str, Any] = {
        "key": "test-model",
        "display_name": "Test Model",
        "litellm_model": "openai/test-model",
        "params": {"temperature": 0},
    }
    base.update(overrides)
    return ModelEntry.model_validate(base)


class Recorder:
    """A stand-in for ``acompletion`` that scripts a sequence of outcomes."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class SleepSpy:
    """Captures backoff delays without actually waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def provider(recorder: Recorder, sleep: SleepSpy | None = None, **kwargs: Any) -> LiteLLMProvider:
    return LiteLLMProvider(
        kwargs.pop("entry", entry()),
        acompletion=recorder,
        sleep=sleep or SleepSpy(),
        base_delay_s=kwargs.pop("base_delay_s", 1.0),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# is_retryable                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 599])
def test_transient_statuses_are_retryable(status: int) -> None:
    assert is_retryable(HttpError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 600])
def test_client_errors_are_not_retryable(status: int) -> None:
    """Retrying an auth or bad-request failure just burns quota."""
    assert is_retryable(HttpError(status)) is False


def test_known_transient_exception_names_are_retryable_without_a_status() -> None:
    assert is_retryable(RateLimitError()) is True


def test_unknown_exceptions_are_not_retryable() -> None:
    assert is_retryable(AuthenticationError()) is False
    assert is_retryable(ValueError("nope")) is False


def test_string_status_codes_are_understood() -> None:
    exc = HttpError(200)
    exc.status_code = "503"  # type: ignore[assignment]  # some vendors send strings
    assert is_retryable(exc) is True


def test_boolean_status_code_is_not_mistaken_for_an_int() -> None:
    """`True` is an int in Python; it must not be read as status 1."""
    exc = RateLimitError()
    exc.status_code = True  # type: ignore[attr-defined]
    # Falls through to the class-name check, which says retryable.
    assert is_retryable(exc) is True


# --------------------------------------------------------------------------- #
# Request assembly                                                             #
# --------------------------------------------------------------------------- #


def test_request_carries_model_params_and_drop_params() -> None:
    built = provider(Recorder()).build_request([{"role": "user", "content": "hi"}], [])
    assert built["model"] == "openai/test-model"
    assert built["temperature"] == 0
    # SPEC 6.4: unsupported params are dropped rather than raising.
    assert built["drop_params"] is True
    # No tools means no tool_choice at all, rather than an empty list.
    assert "tools" not in built
    assert "tool_choice" not in built


def test_tools_enable_auto_choice_and_parallel_calls() -> None:
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    built = provider(Recorder()).build_request([], tools)
    assert built["tools"] == tools
    assert built["tool_choice"] == "auto"
    assert built["parallel_tool_calls"] is True


def test_parallel_tool_calls_omitted_when_the_model_lacks_support() -> None:
    built = provider(Recorder(), entry=entry(supports_parallel_tool_calls=False)).build_request(
        [], [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    )
    assert "parallel_tool_calls" not in built


def test_call_params_override_registry_params() -> None:
    """A run's params_override must win over the registry default."""
    built = provider(Recorder()).build_request([], [], temperature=0.9)
    assert built["temperature"] == 0.9


async def test_complete_passes_the_built_request_through() -> None:
    recorder = Recorder(response())
    await provider(recorder).complete([{"role": "user", "content": "hi"}], [])
    assert recorder.calls[0]["model"] == "openai/test-model"
    assert recorder.calls[0]["messages"] == [{"role": "user", "content": "hi"}]


# --------------------------------------------------------------------------- #
# Retry and backoff                                                            #
# --------------------------------------------------------------------------- #


async def test_retries_a_rate_limit_then_succeeds() -> None:
    recorder = Recorder(HttpError(429), HttpError(429), response("recovered"))
    sleep = SleepSpy()
    message = await provider(recorder, sleep).complete([], [])
    assert message.content == "recovered"
    assert len(recorder.calls) == 3


async def test_backoff_is_exponential() -> None:
    recorder = Recorder(HttpError(503), HttpError(503), HttpError(503), response())
    sleep = SleepSpy()
    await provider(recorder, sleep, base_delay_s=0.5).complete([], [])
    assert sleep.delays == [0.5, 1.0, 2.0]


async def test_gives_up_after_max_attempts() -> None:
    recorder = Recorder(HttpError(429))
    sleep = SleepSpy()
    with pytest.raises(ProviderError, match="after 5 attempt"):
        await provider(recorder, sleep).complete([], [])
    assert len(recorder.calls) == 5
    # One fewer sleep than attempts: the last failure is not followed by a wait.
    assert len(sleep.delays) == 4


async def test_max_attempts_is_configurable() -> None:
    recorder = Recorder(HttpError(500))
    with pytest.raises(ProviderError, match="after 2 attempt"):
        await provider(recorder, SleepSpy(), max_attempts=2).complete([], [])
    assert len(recorder.calls) == 2


async def test_max_attempts_below_one_still_tries_once() -> None:
    recorder = Recorder(response())
    await provider(recorder, SleepSpy(), max_attempts=0).complete([], [])
    assert len(recorder.calls) == 1


async def test_permanent_failures_are_not_retried() -> None:
    """An expired key should fail on the first call, not the fifth."""
    recorder = Recorder(HttpError(401, "bad key"))
    sleep = SleepSpy()
    with pytest.raises(ProviderError, match="after 1 attempt"):
        await provider(recorder, sleep).complete([], [])
    assert len(recorder.calls) == 1
    assert sleep.delays == []


async def test_retries_are_reported_to_the_hook() -> None:
    """Retries happen in the provider but belong in the attempt trace."""
    recorder = Recorder(HttpError(429, "slow down"), response())
    seen: list[dict[str, Any]] = []

    async def hook(**kwargs: Any) -> None:
        seen.append(kwargs)

    p = provider(recorder, SleepSpy())
    p.set_retry_hook(hook)
    await p.complete([], [])

    assert len(seen) == 1
    assert seen[0]["attempt"] == 1
    assert seen[0]["max_attempts"] == 5
    assert seen[0]["delay_s"] == 1.0
    assert seen[0]["status_code"] == 429
    assert "slow down" in seen[0]["error"]


async def test_retry_hook_can_be_removed() -> None:
    recorder = Recorder(HttpError(429), response())
    p = provider(recorder, SleepSpy())
    p.set_retry_hook(None)
    await p.complete([], [])  # must not raise


async def test_provider_error_names_the_model_key() -> None:
    recorder = Recorder(HttpError(400, "malformed"))
    with pytest.raises(ProviderError, match="test-model"):
        await provider(recorder, SleepSpy()).complete([], [])


# --------------------------------------------------------------------------- #
# Response normalization                                                       #
# --------------------------------------------------------------------------- #


async def test_normalizes_content_usage_and_finish_reason() -> None:
    recorder = Recorder(response("hello", prompt_tokens=31, completion_tokens=5))
    message = await provider(recorder).complete([], [])
    assert message.content == "hello"
    assert message.input_tokens == 31
    assert message.output_tokens == 5
    assert message.finish_reason == "stop"
    assert message.latency_ms >= 0


async def test_normalizes_tool_calls() -> None:
    calls = [
        FakeToolCall("call_1", FakeFunction("search_flights", '{"origin": "DXB"}')),
        FakeToolCall("call_2", FakeFunction("get_fare_rules", '{"flight_id": "FL-204"}')),
    ]
    message = await provider(Recorder(response(None, calls))).complete([], [])
    assert [c.name for c in message.tool_calls] == ["search_flights", "get_fare_rules"]
    assert message.tool_calls[0].arguments == {"origin": "DXB"}
    assert message.content is None


async def test_non_string_content_becomes_none() -> None:
    """Some vendors return structured content blocks; the runner wants text or nothing."""
    message = await provider(Recorder(response([{"type": "text"}]))).complete([], [])  # type: ignore[arg-type]
    assert message.content is None


async def test_missing_usage_reports_zero_tokens() -> None:
    recorder = Recorder(FakeResponse(choices=[FakeChoice(message=FakeMessage("hi"))], usage=None))
    message = await provider(recorder).complete([], [])
    assert message.input_tokens == 0
    assert message.output_tokens == 0


async def test_empty_choices_is_an_error() -> None:
    with pytest.raises(ProviderError, match="no choices"):
        await provider(Recorder(FakeResponse(choices=[]))).complete([], [])


async def test_choice_without_a_message_is_an_error() -> None:
    with pytest.raises(ProviderError, match="no message"):
        await provider(Recorder(FakeResponse(choices=[FakeChoice(message=None)]))).complete([], [])


# --------------------------------------------------------------------------- #
# Tool-call parsing                                                            #
# --------------------------------------------------------------------------- #


def test_parses_json_arguments() -> None:
    call = parse_tool_call(FakeToolCall("c1", FakeFunction("book", '{"id": "FL-204"}')), 0)
    assert call.arguments == {"id": "FL-204"}
    assert call.parse_error is None


def test_accepts_arguments_already_decoded() -> None:
    call = parse_tool_call(FakeToolCall("c1", FakeFunction("book", {"id": "FL-204"})), 0)
    assert call.arguments == {"id": "FL-204"}


@pytest.mark.parametrize("empty", [None, ""])
def test_missing_arguments_become_an_empty_object(empty: Any) -> None:
    call = parse_tool_call(FakeToolCall("c1", FakeFunction("list_tables", empty)), 0)
    assert call.arguments == {}
    assert call.parse_error is None


def test_malformed_json_is_preserved_not_raised() -> None:
    """A model emitting bad JSON is a graded behavior, not a crash (SPEC 5.1)."""
    call = parse_tool_call(FakeToolCall("c1", FakeFunction("book", "{not json")), 0)
    assert call.arguments == {}
    assert call.raw_arguments == "{not json"
    assert call.parse_error is not None
    assert "not valid JSON" in call.parse_error


def test_non_object_json_arguments_are_rejected_with_a_reason() -> None:
    call = parse_tool_call(FakeToolCall("c1", FakeFunction("book", "[1, 2]")), 0)
    assert call.arguments == {}
    assert call.parse_error is not None
    assert "must be a JSON object" in call.parse_error
    assert "list" in call.parse_error


def test_missing_call_id_falls_back_to_the_index() -> None:
    call = parse_tool_call(FakeToolCall("", FakeFunction("book", "{}")), 3)
    assert call.id == "call_3"


# --------------------------------------------------------------------------- #
# Cost (SPEC 6.3: never guess)                                                 #
# --------------------------------------------------------------------------- #


def test_pricing_override_is_computed_exactly() -> None:
    p = provider(
        Recorder(),
        entry=entry(pricing_override=Pricing(input_per_mtok=3.0, output_per_mtok=15.0)),
    )
    # 1M input at $3 plus 1M output at $15.
    assert p.cost_for(None, 1_000_000, 1_000_000) == pytest.approx(18.0)


def test_pricing_override_wins_over_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = types.ModuleType("litellm")
    stub.completion_cost = lambda **_: 999.0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", stub)

    p = provider(
        Recorder(), entry=entry(pricing_override=Pricing(input_per_mtok=1.0, output_per_mtok=1.0))
    )
    assert p.cost_for(None, 1_000_000, 0) == pytest.approx(1.0)


def test_cost_comes_from_litellm_when_there_is_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = types.ModuleType("litellm")
    stub.completion_cost = lambda **_: 0.0125  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", stub)

    assert provider(Recorder()).cost_for(object(), 10, 10) == pytest.approx(0.0125)


def test_unknown_pricing_reports_none_rather_than_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC 6.3 is explicit: null when pricing is unknown, never an estimate."""

    def explode(**_: Any) -> float:
        raise RuntimeError("this model has no pricing data")

    stub = types.ModuleType("litellm")
    stub.completion_cost = explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", stub)

    assert provider(Recorder()).cost_for(object(), 10, 10) is None


@pytest.mark.parametrize("bogus", ["1.25", None, True])
def test_non_numeric_costs_are_treated_as_unknown(
    monkeypatch: pytest.MonkeyPatch, bogus: Any
) -> None:
    stub = types.ModuleType("litellm")
    stub.completion_cost = lambda **_: bogus  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", stub)

    assert provider(Recorder()).cost_for(object(), 10, 10) is None


async def test_cost_reaches_the_assistant_message() -> None:
    p = provider(
        Recorder(response(prompt_tokens=1_000_000, completion_tokens=0)),
        entry=entry(pricing_override=Pricing(input_per_mtok=2.0, output_per_mtok=8.0)),
    )
    message = await p.complete([], [])
    assert message.cost_usd == pytest.approx(2.0)


# --------------------------------------------------------------------------- #
# Provider identity                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("litellm_model", "expected"),
    [
        ("openai/gpt-4o", "openai"),
        ("anthropic/claude-opus-5", "anthropic"),
        ("gemini/gemini-2.0-flash", "gemini"),
        ("bare-model-name", "litellm"),
    ],
)
def test_provider_name_drives_the_concurrency_bucket(litellm_model: str, expected: str) -> None:
    """The name is the per-provider rate-limit key (SPEC 9.1)."""
    assert provider(Recorder(), entry=entry(litellm_model=litellm_model)).name == expected


# --------------------------------------------------------------------------- #
# Retry-After: the provider knows better than exponential backoff              #
# --------------------------------------------------------------------------- #


class HeaderError(Exception):
    """A vendor error carrying a Retry-After header, as httpx surfaces it."""

    def __init__(self, retry_after: str) -> None:
        super().__init__("rate limited")
        self.status_code = 429
        self.response = types.SimpleNamespace(headers={"retry-after": retry_after})


def test_retry_after_header_is_used() -> None:
    assert retry_after_seconds(HeaderError("12")) == pytest.approx(12.0)


def test_retry_after_header_tolerates_a_seconds_suffix() -> None:
    assert retry_after_seconds(HeaderError("8s")) == pytest.approx(8.0)


def test_a_nonsense_header_falls_through_to_the_message() -> None:
    exc = HeaderError("soon")
    exc.args = ("rate limited. Please try again in 3s.",)
    assert retry_after_seconds(exc) == pytest.approx(3.0)


def test_seconds_hint_is_parsed_from_the_message_body() -> None:
    """Groq states the wait in prose rather than a header."""
    exc = HttpError(429, "Rate limit reached. Please try again in 7.2375s. Upgrade?")
    assert retry_after_seconds(exc) == pytest.approx(7.2375)


def test_millisecond_hint_is_parsed() -> None:
    exc = HttpError(429, "Please try again in 764.999999ms. Upgrade?")
    assert retry_after_seconds(exc) == pytest.approx(0.765, abs=1e-3)


def test_no_hint_reports_none() -> None:
    assert retry_after_seconds(HttpError(500, "internal error")) is None


def test_an_absurd_hint_is_capped() -> None:
    """A provider asking for an hour must not wedge the run."""
    exc = HttpError(429, "Please try again in 9999s.")
    assert retry_after_seconds(exc) == pytest.approx(MAX_RETRY_DELAY_S)


async def test_backoff_waits_as_long_as_the_provider_asked() -> None:
    """The bug this fixes: blind backoff undershot a TPM limit every time,
    spending all five attempts without once waiting long enough."""
    recorder = Recorder(HttpError(429, "Please try again in 7.2375s."), response())
    sleep = SleepSpy()
    await provider(recorder, sleep, base_delay_s=1.0).complete([], [])

    assert len(sleep.delays) == 1
    # The hint plus a small margin, not the 1s exponential step.
    assert sleep.delays[0] == pytest.approx(7.7375)


async def test_exponential_backoff_wins_when_it_is_longer() -> None:
    recorder = Recorder(
        HttpError(429, "Please try again in 0.1s."),
        HttpError(429, "Please try again in 0.1s."),
        HttpError(429, "Please try again in 0.1s."),
        response(),
    )
    sleep = SleepSpy()
    await provider(recorder, sleep, base_delay_s=4.0).complete([], [])
    assert sleep.delays == [4.0, 8.0, 16.0]


async def test_a_hint_never_exceeds_the_cap_during_a_retry() -> None:
    recorder = Recorder(HttpError(429, "Please try again in 9999s."), response())
    sleep = SleepSpy()
    await provider(recorder, sleep).complete([], [])
    assert sleep.delays[0] == pytest.approx(MAX_RETRY_DELAY_S)
