"""Application settings (SPEC 7, 9.1, 11).

Every knob comes from the environment; the names match ``.env.example`` exactly.
Defaults resolve to the repo checkout so the CLI works with no containers and no
``.env`` file at all, which is what ``evalharness validate`` and ``run --no-db``
need in CI.
"""

from __future__ import annotations

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
