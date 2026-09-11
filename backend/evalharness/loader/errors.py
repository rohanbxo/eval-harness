"""Loader errors that point at the offending file, path and reason (SPEC 13, Phase 1)."""

from __future__ import annotations

from pathlib import Path


class ScenarioValidationError(Exception):
    """A scenario failed to load. Carries enough detail to fix it without guessing."""

    def __init__(
        self,
        file: Path | str,
        reason: str,
        *,
        path: str | None = None,
        hint: str | None = None,
    ) -> None:
        self.file = str(file)
        self.reason = reason
        self.path = path
        self.hint = hint
        super().__init__(str(self))

    def __str__(self) -> str:
        location = f"{self.file}" + (f" at {self.path}" if self.path else "")
        message = f"{location}: {self.reason}"
        return message + (f"\n  hint: {self.hint}" if self.hint else "")


class RegistryError(Exception):
    """The model registry could not be loaded."""
