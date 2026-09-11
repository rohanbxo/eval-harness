"""Model registry configuration (SPEC 7)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Pricing(BaseModel):
    """Manual pricing override, USD per million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)


class ModelEntry(BaseModel):
    """One entry in ``config/models.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", description="Stable key for UI and CLI.")
    display_name: str
    litellm_model: str
    params: dict[str, Any] = Field(default_factory=dict)
    supports_parallel_tool_calls: bool = True
    supports_tool_calling: bool = Field(
        default=True, description="False marks the model unsupported; runs are skipped."
    )
    api_key_env: str | None = Field(
        default=None, description="Env var that must be set. Inferred from the provider if omitted."
    )
    pricing_override: Pricing | None = None


class ModelRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    models: list[ModelEntry] = Field(min_length=1)

    def get(self, key: str) -> ModelEntry:
        for entry in self.models:
            if entry.key == key:
                return entry
        known = ", ".join(sorted(m.key for m in self.models))
        raise KeyError(f"unknown model key {key!r}; registry has: {known}")
