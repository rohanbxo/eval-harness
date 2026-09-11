"""The conversation loop and the provider adapters (SPEC 6.1, 6.4)."""

from evalharness.runner.conversation import (
    AttemptContext,
    build_system_prompt,
    format_clock,
    run_attempt,
)
from evalharness.runner.fake_model import FakeModel, TranscriptExhaustedError
from evalharness.runner.litellm_provider import LiteLLMProvider, is_retryable
from evalharness.runner.provider import Provider, ProviderError, RetryHook, RetryReporting

__all__ = [
    "AttemptContext",
    "FakeModel",
    "LiteLLMProvider",
    "Provider",
    "ProviderError",
    "RetryHook",
    "RetryReporting",
    "TranscriptExhaustedError",
    "build_system_prompt",
    "format_clock",
    "is_retryable",
    "run_attempt",
]
