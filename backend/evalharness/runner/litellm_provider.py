"""LiteLLM-backed provider (SPEC 6.4, 7).

LiteLLM normalizes every vendor to OpenAI-style tool calls, so this adapter is
thin: assemble the request from the registry entry, retry the transient
failures, and normalize the response into an ``AssistantMessage``.

``litellm`` is imported lazily -- importing it costs seconds, and the CLI must
stay usable (``validate``, ``list-models``) without paying that.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from evalharness.runner.provider import ProviderError, RetryHook
from evalharness.schema.registry import ModelEntry
from evalharness.schema.runtime import AssistantMessage, ToolCall

#: Non-5xx statuses worth retrying: rate limits and request timeouts (SPEC 6.4).
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 409, 429})

#: LiteLLM exception class names that mean "transient", for the cases where the
#: exception carries no status code at all (connection resets, timeouts).
_RETRYABLE_EXCEPTIONS: frozenset[str] = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
        "ServiceUnavailableError",
        "Timeout",
    }
)


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)
    return None


def is_retryable(exc: BaseException) -> bool:
    """429 and 5xx are retryable; everything else (auth, bad request) is not."""
    code = _status_code(exc)
    if code is not None:
        return code in _RETRYABLE_STATUSES or 500 <= code < 600
    return type(exc).__name__ in _RETRYABLE_EXCEPTIONS


class LiteLLMProvider:
    """Wraps ``litellm.acompletion`` for one registry entry."""

    def __init__(
        self,
        entry: ModelEntry,
        *,
        max_attempts: int = 5,
        base_delay_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        acompletion: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.entry = entry
        self.name = (
            entry.litellm_model.split("/", 1)[0] if "/" in entry.litellm_model else "litellm"
        )
        self.max_attempts = max(1, max_attempts)
        self.base_delay_s = base_delay_s
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._acompletion = acompletion
        self._retry_hook: RetryHook | None = None

    def set_retry_hook(self, hook: RetryHook | None) -> None:
        """Install the runner's ``retry`` event recorder (SPEC 6.4)."""
        self._retry_hook = hook

    # -- request ---------------------------------------------------------

    def _completion_fn(self) -> Callable[..., Awaitable[Any]]:
        if self._acompletion is not None:
            return self._acompletion
        import litellm  # deliberately lazy: see the module docstring

        # Unsupported params (temperature on some reasoning models) are dropped
        # rather than raising -- SPEC 6.4.
        litellm.drop_params = True
        fn: Callable[..., Awaitable[Any]] = litellm.acompletion
        return fn

    def build_request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **params: Any
    ) -> dict[str, Any]:
        """The kwargs handed to ``acompletion``, registry params included."""
        request: dict[str, Any] = {
            "model": self.entry.litellm_model,
            "messages": messages,
            "drop_params": True,
            **self.entry.params,
            **params,
        }
        if tools:
            request["tools"] = tools
            request.setdefault("tool_choice", "auto")
            if self.entry.supports_parallel_tool_calls:
                request["parallel_tool_calls"] = True
        return request

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **params: Any
    ) -> AssistantMessage:
        request = self.build_request(messages, tools, **params)
        completion = self._completion_fn()

        started = time.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await completion(**request)
            except Exception as exc:
                if attempt == self.max_attempts or not is_retryable(exc):
                    raise ProviderError(
                        f"{self.entry.key}: model call failed after {attempt} attempt(s): "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                delay = self.base_delay_s * (2 ** (attempt - 1))
                if self._retry_hook is not None:
                    await self._retry_hook(
                        attempt=attempt,
                        max_attempts=self.max_attempts,
                        delay_s=delay,
                        error=f"{type(exc).__name__}: {exc}",
                        status_code=_status_code(exc),
                    )
                await self._sleep(delay)
                continue
            latency_ms = int((time.monotonic() - started) * 1000)
            return self._normalize(response, latency_ms)

        raise ProviderError(f"{self.entry.key}: model call failed")  # pragma: no cover

    # -- response --------------------------------------------------------

    def _normalize(self, response: Any, latency_ms: int) -> AssistantMessage:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ProviderError(f"{self.entry.key}: model returned no choices")
        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise ProviderError(f"{self.entry.key}: model returned a choice with no message")

        content = getattr(message, "content", None)
        tool_calls = [
            parse_tool_call(raw, index)
            for index, raw in enumerate(getattr(message, "tool_calls", None) or [])
        ]
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)

        return AssistantMessage(
            content=content if isinstance(content, str) else None,
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", None),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.cost_for(response, input_tokens, output_tokens),
            latency_ms=latency_ms,
        )

    def cost_for(self, response: Any, input_tokens: int, output_tokens: int) -> float | None:
        """USD for one call, or ``None`` when pricing is unknown (SPEC 6.3).

        A registry ``pricing_override`` wins; otherwise LiteLLM's cost tracking
        is asked. Nothing is ever estimated: an unpriced model reports ``None``
        so the dashboard can say "unknown" instead of showing a made-up number.
        """
        override = self.entry.pricing_override
        if override is not None:
            return (
                input_tokens * override.input_per_mtok + output_tokens * override.output_per_mtok
            ) / 1_000_000

        try:
            import litellm

            cost = litellm.completion_cost(completion_response=response)
        except Exception:
            return None
        if isinstance(cost, bool) or not isinstance(cost, int | float):
            return None
        return float(cost)


def parse_tool_call(raw: Any, index: int) -> ToolCall:
    """Normalize one vendor tool call, keeping malformed JSON instead of raising.

    A model that emits unparseable arguments is exhibiting a graded behavior, so
    the bad payload is preserved and handed back as an error (SPEC 5.1) rather
    than crashing the attempt.
    """
    function = getattr(raw, "function", None)
    name = str(getattr(function, "name", "") or "")
    arguments = getattr(function, "arguments", None)
    call_id = str(getattr(raw, "id", "") or f"call_{index}")

    if arguments is None or arguments == "":
        return ToolCall(id=call_id, name=name, arguments={})
    if isinstance(arguments, dict):
        return ToolCall(id=call_id, name=name, arguments=arguments)
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError) as exc:
        return ToolCall(
            id=call_id,
            name=name,
            arguments={},
            raw_arguments=str(arguments),
            parse_error=f"arguments are not valid JSON: {exc}",
        )
    if not isinstance(parsed, dict):
        return ToolCall(
            id=call_id,
            name=name,
            arguments={},
            raw_arguments=str(arguments),
            parse_error=f"arguments must be a JSON object, got {type(parsed).__name__}",
        )
    return ToolCall(id=call_id, name=name, arguments=parsed, raw_arguments=str(arguments))
