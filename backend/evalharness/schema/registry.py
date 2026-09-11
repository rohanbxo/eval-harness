"""Model registry configuration (SPEC 7)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Pricing(BaseModel):
    """Manual pricing override, USD per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class ProviderRouting(BaseModel):
    """OpenRouter provider pinning (SPEC 7, extended -- see DECISIONS D20).

    OpenRouter serves one model slug from many hosts, which differ in
    quantization, context window and tool-calling fidelity. Left to itself it
    picks by price and availability, so two runs of "the same model" can be
    served by different hardware and the comparison quietly stops being fair.
    Pinning the order with fallbacks off makes a run reproducible or makes it
    fail loudly, rather than silently substituting a host.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    order: list[str] = Field(
        min_length=1,
        description="Provider slugs in preference order, e.g. ['openai'].",
    )
    allow_fallbacks: bool = Field(
        default=False,
        description="False pins the run to `order`; a miss is an error, not a substitution.",
    )
    require_parameters: bool = Field(
        default=True,
        description="Skip hosts that cannot honor the request's params (tools, reasoning).",
    )
    quantizations: list[str] | None = Field(
        default=None,
        description="Restrict to these quantization levels, e.g. ['fp8'] or ['bf16'].",
    )

    def to_openrouter(self) -> dict[str, Any]:
        """The `provider` block OpenRouter expects in the request body."""
        block: dict[str, Any] = {
            "order": list(self.order),
            "allow_fallbacks": self.allow_fallbacks,
            "require_parameters": self.require_parameters,
        }
        if self.quantizations is not None:
            block["quantizations"] = list(self.quantizations)
        return block


class ModelEntry(BaseModel):
    """One entry in ``config/models.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", description="Stable key for UI and CLI.")
    display_name: str
    litellm_model: str
    params: dict[str, Any] = Field(default_factory=dict)
    rpm: int = Field(
        default=15,
        ge=0,
        description=(
            "Requests per minute this model may make. Providers cap per model, so "
            "each is throttled independently and models still run in parallel. "
            "0 disables throttling (DECISIONS D27)."
        ),
    )
    supports_parallel_tool_calls: bool = True
    supports_tool_calling: bool = Field(
        default=True, description="False marks the model unsupported; runs are skipped."
    )
    api_key_env: str | None = Field(
        default=None, description="Env var that must be set. Inferred from the provider if omitted."
    )
    pricing_override: Pricing | None = None
    provider_routing: ProviderRouting | None = Field(
        default=None,
        description="OpenRouter provider pinning. Ignored by other providers.",
    )
    reasoning_effort: str | None = Field(
        default=None,
        description=(
            "Requested reasoning effort, already mapped to a value this model "
            "supports (DeepSeek V4 has no 'medium', so it takes 'high')."
        ),
    )

    def effective_params(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Every param that will actually be sent, recorded on the run.

        Built here rather than in the provider so the run record and the request
        cannot drift apart.
        """
        params: dict[str, Any] = dict(self.params)
        if self.reasoning_effort is not None:
            params["reasoning_effort"] = self.reasoning_effort
        if self.provider_routing is not None:
            params["provider"] = self.provider_routing.to_openrouter()
        params.update(overrides or {})
        return params


class ModelRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    models: list[ModelEntry] = Field(min_length=1)

    def get(self, key: str) -> ModelEntry:
        for entry in self.models:
            if entry.key == key:
                return entry
        known = ", ".join(sorted(m.key for m in self.models))
        raise KeyError(f"unknown model key {key!r}; registry has: {known}")
