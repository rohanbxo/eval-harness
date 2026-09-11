"""FastAPI dependencies: settings, database sessions, scenario and model caches.

Scenario definitions live on disk, so the API caches them for a few seconds
rather than re-reading five directories per request -- short enough that editing
a scenario shows up without restarting the API, long enough that a dashboard
refresh does not re-hash every fixture.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from evalharness.db.session import get_database
from evalharness.loader import LoadedScenario, ScenarioValidationError, load_scenario
from evalharness.schema.registry import ModelEntry, ModelRegistry

#: How long loaded scenarios and the model registry are reused.
CACHE_TTL_SECONDS = 5.0

#: Fallback env var names per provider prefix, used when a registry entry omits
#: ``api_key_env``. Standard LiteLLM names (SPEC 7).
PROVIDER_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "vertex_ai": "GOOGLE_APPLICATION_CREDENTIALS",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "together_ai": "TOGETHERAI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "bedrock": "AWS_ACCESS_KEY_ID",
}


def settings() -> Any:
    """The app settings object (owned by ``evalharness.config``)."""
    from evalharness.config import get_settings

    return get_settings()


def scenarios_dir() -> Path:
    return Path(str(settings().scenarios_dir))


def models_file() -> Path:
    return Path(str(settings().models_file))


# ---------------------------------------------------------------- scenario cache


@dataclass(frozen=True)
class ScenarioEntry:
    """A scenario directory and how loading it went (the UI shows both)."""

    id: str
    loaded: LoadedScenario | None
    error: str | None = None

    @property
    def valid(self) -> bool:
        return self.loaded is not None

    @property
    def config_hash(self) -> str:
        return self.loaded.config_hash if self.loaded else ""


class _TtlCache:
    def __init__(self, ttl: float = CACHE_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._value: Any = None
        self._loaded_at = 0.0

    def get(self, build: Any) -> Any:
        if self._value is None or time.monotonic() - self._loaded_at > self.ttl:
            self._value = build()
            self._loaded_at = time.monotonic()
        return self._value

    def clear(self) -> None:
        self._value = None
        self._loaded_at = 0.0


_scenarios = _TtlCache()
_registry = _TtlCache()


def _load_scenarios() -> dict[str, ScenarioEntry]:
    root = scenarios_dir()
    entries: dict[str, ScenarioEntry] = {}
    if not root.is_dir():
        return entries
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (directory / "scenario.yaml").is_file():
            continue
        try:
            entries[directory.name] = ScenarioEntry(directory.name, load_scenario(directory))
        except ScenarioValidationError as exc:
            # A broken scenario must not hide the healthy ones: it is listed as
            # invalid with its error, which is what /scenarios reports.
            entries[directory.name] = ScenarioEntry(directory.name, None, str(exc))
        except (OSError, ValueError) as exc:
            entries[directory.name] = ScenarioEntry(directory.name, None, str(exc))
    return entries


def get_scenarios() -> dict[str, ScenarioEntry]:
    result: dict[str, ScenarioEntry] = _scenarios.get(_load_scenarios)
    return result


def get_scenario_or_404(scenario_id: str) -> ScenarioEntry:
    entry = get_scenarios().get(scenario_id)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown scenario {scenario_id!r}")
    return entry


def get_registry() -> ModelRegistry:
    result: ModelRegistry = _registry.get(lambda: _load_registry())
    return result


def _load_registry() -> ModelRegistry:
    from evalharness.loader import load_registry

    return load_registry(models_file())


def reset_caches() -> None:
    """Drop cached scenarios and registry (used by tests and after config edits)."""
    _scenarios.clear()
    _registry.clear()


def api_key_env_for(entry: ModelEntry) -> str | None:
    """The env var this model needs, explicit or inferred from its provider."""
    if entry.api_key_env is not None:
        return entry.api_key_env
    provider = entry.litellm_model.split("/", 1)[0]
    if provider == "fake":
        return None
    return PROVIDER_KEY_ENV.get(provider)


def api_key_present(entry: ModelEntry) -> bool:
    """Whether the key is available. The value itself is never read out of here."""
    env = api_key_env_for(entry)
    if env is None:
        return True  # nothing to configure (FakeModel, or a keyless local model)
    return bool(os.environ.get(env))


# ------------------------------------------------------------------- db sessions


async def get_session() -> AsyncIterator[AsyncSession]:
    """A request-scoped session that commits on success."""
    async with get_database().session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
LimitDep = Annotated[int, Query(ge=1, le=200)]
OffsetDep = Annotated[int, Query(ge=0)]
