"""Application settings (SPEC 7, 9.1, 11).

Every knob comes from the environment; the names match ``.env.example`` exactly.
Defaults resolve to the repo checkout so the CLI works with no containers and no
``.env`` file at all, which is what ``evalharness validate`` and ``run --no-db``
need in CI.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repo root: ``<root>/backend/evalharness/config.py`` -> ``<root>``.
REPO_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Harness configuration, read from the environment."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    scenarios_dir: Path = Field(
        default=REPO_ROOT / "scenarios",
        validation_alias=AliasChoices("EVALHARNESS_SCENARIOS_DIR", "scenarios_dir"),
        description="Directory holding one sub-directory per scenario.",
    )
    models_file: Path = Field(
        default=REPO_ROOT / "config" / "models.yaml",
        validation_alias=AliasChoices("EVALHARNESS_MODELS_FILE", "models_file"),
        description="Model registry (SPEC 7).",
    )
    database_url: str = Field(
        default="postgresql+asyncpg://evalharness:evalharness@localhost:5432/evalharness",
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        validation_alias=AliasChoices("REDIS_URL", "redis_url"),
    )
    provider_concurrency: int = Field(
        default=4,
        ge=1,
        validation_alias=AliasChoices("EVALHARNESS_PROVIDER_CONCURRENCY", "provider_concurrency"),
        description="Max concurrent model calls per provider (SPEC 9.1).",
    )
    enable_judge: bool = Field(
        default=False,
        validation_alias=AliasChoices("EVALHARNESS_ENABLE_JUDGE", "enable_judge"),
        description="LLM-judge assertions are non-deterministic and off unless enabled.",
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("LOG_LEVEL", "log_level"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once."""
    return Settings()


def export_dotenv(path: Path | None = None) -> list[str]:
    """Copy ``.env`` into ``os.environ`` and return the names that were set.

    ``Settings`` reads ``.env`` for the harness's own knobs, but provider
    credentials are read straight from ``os.environ`` by LiteLLM, which never
    sees pydantic's view of the file. Without this, the documented local flow --
    put ``GROQ_API_KEY`` in ``.env``, then ``evalharness run`` -- fails to
    authenticate, even though the same file works fine under docker compose
    (there, ``env_file`` does this job).

    A variable already present in the environment always wins, so an explicitly
    exported key beats the file. Blank entries are skipped: ``.env.example``
    ships every provider key as an empty placeholder, and exporting those as
    empty strings only risks a provider SDK treating one as a real credential.
    Only names are returned; values are never returned or logged.
    """
    from dotenv import dotenv_values

    env_path = path if path is not None else REPO_ROOT / ".env"
    if not env_path.is_file():
        return []

    exported: list[str] = []
    for name, value in dotenv_values(env_path).items():
        if not value or name in os.environ:
            continue
        os.environ[name] = value
        exported.append(name)
    return exported
