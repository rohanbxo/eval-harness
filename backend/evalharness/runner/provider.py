"""The provider boundary (SPEC 6.4).

Everything above this line speaks ``AssistantMessage``; everything below it
speaks whatever the vendor speaks. Two implementations ship: ``FakeModel``
(scripted transcripts, no network) and ``LiteLLMProvider``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from evalharness.schema.runtime import AssistantMessage


class ProviderError(RuntimeError):
    """A model call failed in a way the runner cannot recover from."""


@runtime_checkable
class Provider(Protocol):
    """Anything that can turn a message list into one assistant message."""

    name: str

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        call_timeout_s: float | None = None,
        **params: Any,
    ) -> AssistantMessage:
        """Return the next assistant message for ``messages``.

        ``messages`` is OpenAI-shaped; ``tools`` are OpenAI function schemas with
        the scenario's ``mock`` config already stripped.

        ``call_timeout_s`` bounds **the model call itself** and nothing else. A
        provider that queues for a rate-limit slot, or backs off after a 429,
        must not spend that budget on waiting: the caller is measuring how long
        the model took, not how long the harness throttled it (DECISIONS D33).
        Exceeding it raises :class:`TimeoutError`.
        """
        ...


@runtime_checkable
class RetryHook(Protocol):
    """Called by a provider each time it backs off and retries (SPEC 6.4)."""

    async def __call__(
        self,
        *,
        attempt: int,
        max_attempts: int,
        delay_s: float,
        error: str,
        status_code: int | None,
    ) -> None: ...


@runtime_checkable
class RetryReporting(Protocol):
    """A provider that can report its retries to the attempt's event log.

    Retries happen inside the provider but belong in the attempt trace, so the
    runner installs a hook when the provider offers one. Providers that do not
    retry (``FakeModel``) simply do not implement this.
    """

    def set_retry_hook(self, hook: RetryHook | None) -> None: ...


@runtime_checkable
class RateLimited(Protocol):
    """A provider whose every HTTP request should take a rate-limiter slot.

    The hook is installed on the provider rather than called around it because
    the retry loop lives inside: throttling the outside let one slot cover up to
    five real requests (DECISIONS D32).
    """

    def set_rate_limit_hook(
        self, hook: Callable[[], Awaitable[tuple[float, bool]]] | None
    ) -> None: ...
