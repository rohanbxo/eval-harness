"""Provider pinning, served-provider recording, and the spend guard.

OpenRouter fronts many hosts behind one model slug, and they differ in
quantization and tool-calling fidelity. These tests pin the three properties a
fair comparison needs: the pin is actually sent, the host that served each call
is recorded, and a run stops at its budget rather than quietly overspending.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from evalharness.cli import DEFAULT_MAX_COST_USD, _budget_stop
from evalharness.schema.registry import ModelEntry, ProviderRouting

#: Resolved at import time: touching the filesystem inside an async test would
#: block its event loop (ruff ASYNC240).
TRAVEL_BOOKING = Path(__file__).resolve().parents[2] / "scenarios" / "travel-booking"


@pytest.fixture(autouse=True)
def _no_real_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = types.ModuleType("litellm")
    stub.completion_cost = lambda **_: None  # type: ignore[attr-defined]
    stub.drop_params = False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", stub)


def entry(**overrides: Any) -> ModelEntry:
    base: dict[str, Any] = {
        "key": "pinned",
        "display_name": "Pinned",
        "litellm_model": "openrouter/openai/gpt-5.6-terra",
        "params": {"temperature": 0},
    }
    base.update(overrides)
    return ModelEntry.model_validate(base)


# --------------------------------------------------------------------------- #
# The routing block                                                            #
# --------------------------------------------------------------------------- #


def test_fallbacks_are_off_by_default() -> None:
    """A pin that silently falls back to another host is not a pin."""
    routing = ProviderRouting(order=["openai"])
    assert routing.allow_fallbacks is False
    assert routing.require_parameters is True


def test_routing_block_matches_openrouter_shape() -> None:
    block = ProviderRouting(order=["groq", "together"]).to_openrouter()
    assert block == {
        "order": ["groq", "together"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_quantizations_are_included_only_when_set() -> None:
    assert "quantizations" not in ProviderRouting(order=["openai"]).to_openrouter()
    pinned = ProviderRouting(order=["deepseek"], quantizations=["bf16"]).to_openrouter()
    assert pinned["quantizations"] == ["bf16"]


def test_an_empty_order_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        ProviderRouting(order=[])


# --------------------------------------------------------------------------- #
# Effective params                                                             #
# --------------------------------------------------------------------------- #


def test_effective_params_fold_in_reasoning_and_routing() -> None:
    params = entry(
        reasoning_effort="high", provider_routing={"order": ["deepseek"]}
    ).effective_params()
    assert params["temperature"] == 0
    assert params["reasoning_effort"] == "high"
    assert params["provider"]["order"] == ["deepseek"]


def test_effective_params_omit_what_was_not_configured() -> None:
    params = entry().effective_params()
    assert "reasoning_effort" not in params
    assert "provider" not in params


def test_overrides_win_over_the_registry() -> None:
    params = entry(reasoning_effort="medium").effective_params({"reasoning_effort": "xhigh"})
    assert params["reasoning_effort"] == "xhigh"


# --------------------------------------------------------------------------- #
# The request that actually goes out                                           #
# --------------------------------------------------------------------------- #


def build(entry_: ModelEntry, **params: Any) -> dict[str, Any]:
    from evalharness.runner.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(entry_).build_request([{"role": "user", "content": "hi"}], [], **params)


def test_routing_travels_in_extra_body() -> None:
    """OpenRouter reads `provider` from the body, not as an OpenAI param."""
    request = build(entry(provider_routing={"order": ["openai"]}))
    assert request["extra_body"]["provider"]["order"] == ["openai"]
    assert request["extra_body"]["provider"]["allow_fallbacks"] is False
    # It must not leak into the top level, where LiteLLM would reject it.
    assert "provider" not in request


def test_reasoning_effort_is_sent_as_a_normal_param() -> None:
    assert build(entry(reasoning_effort="high"))["reasoning_effort"] == "high"


def test_an_unpinned_model_sends_no_extra_body() -> None:
    assert "extra_body" not in build(entry())


def test_pinning_survives_alongside_tools() -> None:
    from evalharness.runner.litellm_provider import LiteLLMProvider

    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    request = LiteLLMProvider(entry(provider_routing={"order": ["groq"]})).build_request([], tools)
    assert request["tools"] == tools
    assert request["tool_choice"] == "auto"
    assert request["extra_body"]["provider"]["order"] == ["groq"]


# --------------------------------------------------------------------------- #
# Which host served the response                                               #
# --------------------------------------------------------------------------- #


def test_served_provider_is_read_from_the_response() -> None:
    from evalharness.runner.litellm_provider import served_provider

    assert served_provider(types.SimpleNamespace(provider="Groq")) == "Groq"


def test_served_provider_falls_back_to_hidden_params() -> None:
    from evalharness.runner.litellm_provider import served_provider

    response = types.SimpleNamespace(_hidden_params={"provider": "DeepSeek"})
    assert served_provider(response) == "DeepSeek"


def test_served_provider_is_none_when_unreported() -> None:
    """A gateway that says nothing must not produce a fabricated attribution."""
    from evalharness.runner.litellm_provider import served_provider

    assert served_provider(types.SimpleNamespace()) is None
    assert served_provider(types.SimpleNamespace(provider="")) is None


async def test_the_served_provider_reaches_the_assistant_message() -> None:
    from evalharness.runner.litellm_provider import LiteLLMProvider

    response = types.SimpleNamespace(
        provider="Groq",
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content="hi", tool_calls=[]),
                finish_reason="stop",
            )
        ],
        usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )

    async def acompletion(**_: Any) -> Any:
        return response

    message = await LiteLLMProvider(entry(), acompletion=acompletion).complete([], [])
    assert message.provider == "Groq"


# --------------------------------------------------------------------------- #
# The spend guard                                                              #
# --------------------------------------------------------------------------- #


def test_the_default_ceiling_is_two_dollars() -> None:
    assert DEFAULT_MAX_COST_USD == 2.00


@pytest.mark.parametrize(
    ("spent", "limit", "stop"),
    [
        (0.0, 2.0, False),
        (1.99, 2.0, False),
        (2.0, 2.0, False),  # at the limit is still within budget
        (2.01, 2.0, True),
        (99.0, None, False),  # no limit configured
    ],
)
def test_budget_stop_triggers_only_past_the_limit(
    spent: float, limit: float | None, stop: bool
) -> None:
    assert _budget_stop(spent, limit) is stop


async def test_remaining_attempts_are_errored_not_failed_when_the_budget_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget stop is missing data, not a model failure (D19)."""
    import evalharness.cli as cli_module
    from evalharness.cli import _run_attempts
    from evalharness.loader import load_scenario
    from evalharness.schema.runtime import AssistantMessage

    loaded = load_scenario(TRAVEL_BOOKING)

    class PriceyProvider:
        """Every attempt costs more than the whole ceiling."""

        name = "pricey"

        async def complete(self, messages: Any, tools: Any, **params: Any) -> AssistantMessage:
            return AssistantMessage(content="done", cost_usd=5.0)

    monkeypatch.setattr(cli_module, "_provider_for", lambda entry_, loaded_: PriceyProvider())
    results = await _run_attempts(entry(), [loaded], k=3, max_cost_usd=1.0)

    assert len(results) == 3
    # The first ran; the rest were never attempted.
    assert results[0].errored is False
    assert all(r.errored for r in results[1:])
    assert all("budget stop" in (r.error or "") for r in results[1:])


async def test_no_budget_means_every_attempt_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    import evalharness.cli as cli_module
    from evalharness.cli import _run_attempts
    from evalharness.loader import load_scenario, load_transcripts
    from evalharness.runner import FakeModel

    loaded = load_scenario(TRAVEL_BOOKING)
    golden = load_transcripts(TRAVEL_BOOKING)["golden"]

    monkeypatch.setattr(cli_module, "_provider_for", lambda entry_, loaded_: FakeModel(golden))
    results = await _run_attempts(entry(), [loaded], k=2, max_cost_usd=None)

    assert len(results) == 2
    assert all(not r.errored for r in results)
