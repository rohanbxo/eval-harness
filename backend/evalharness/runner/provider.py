"""The provider boundary (SPEC 6.4).

Everything above this line speaks ``AssistantMessage``; everything below it
speaks whatever the vendor speaks. Two implementations ship: ``FakeModel``
(scripted transcripts, no network) and ``LiteLLMProvider``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from evalharness.schema.runtime import AssistantMessage


class ProviderError(RuntimeError):
    """A model call failed in a way the runner cannot recover from."""


@runtime_checkable
class Provider(Protocol):
    """Anything that can turn a message list into one assistant message."""

    name: str

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **params: Any
    ) -> AssistantMessage:
        """Return the next assistant message for ``messages``.

        ``messages`` is OpenAI-shaped; ``tools`` are OpenAI function schemas with
        the scenario's ``mock`` config already stripped.
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
