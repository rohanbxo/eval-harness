"""Pre-flight manifest for a comparison run (DECISIONS D30).

Two ways a run can quietly describe itself wrongly, both of which have already
happened here:

* it records a commit while the working tree holds uncommitted changes, so the
  commit named is not the code that ran;
* the params it sends drift from what ``config/models.yaml`` resolves to, which
  is how a run once went out with temperature pinned on half the field after a
  ``git filter-repo`` reverted an uncommitted edit (D29).

Both are cheap to check before spending money, and expensive to discover after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evalharness.loader import is_dirty, load_registry
from evalharness.loader.hashing import provenance_is_known
from evalharness.schema.registry import ModelEntry


class ManifestError(RuntimeError):
    """A pre-flight check failed; the run must not start."""


@dataclass(frozen=True)
class ModelManifest:
    """What one model will actually be asked to do."""

    key: str
    litellm_model: str
    params: dict[str, Any]
    reasoning_effort: str | None
    rpm: int
    pinned_provider: list[str]
    allow_fallbacks: bool

    @classmethod
    def of(cls, entry: ModelEntry) -> ModelManifest:
        routing = entry.provider_routing
        return cls(
            key=entry.key,
            litellm_model=entry.litellm_model,
            params=entry.effective_params(),
            reasoning_effort=entry.reasoning_effort,
            rpm=entry.rpm,
            pinned_provider=list(routing.order) if routing else [],
            allow_fallbacks=routing.allow_fallbacks if routing else True,
        )

    def render(self) -> str:
        pin = ",".join(self.pinned_provider) or "-"
        params = json.dumps({k: v for k, v in self.params.items() if k != "provider"})
        return (
            f"  {self.key:<20} {self.litellm_model:<42}\n"
            f"      pin={pin:<18} fallbacks={self.allow_fallbacks!s:<6} "
            f"rpm={self.rpm:<4} reasoning={self.reasoning_effort or '-'}\n"
            f"      params={params}"
        )


@dataclass(frozen=True)
class RunManifest:
    """Everything a run's identity depends on, checked before it starts."""

    models: list[ModelManifest]
    git_commit: str
    dirty: bool
    provenance_known: bool = True
    """False when neither a build stamp nor a git checkout could identify the
    code. Distinct from ``dirty``: unknown is not clean (DECISIONS D41)."""
    scenario_hashes: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        if not self.provenance_known:
            marker = "  [PROVENANCE UNKNOWN]"
        elif self.dirty:
            marker = "  [DIRTY]"
        else:
            marker = ""
        lines = [
            "params manifest",
            f"  commit: {self.git_commit}{marker}",
        ]
        for scenario_id, digest in sorted(self.scenario_hashes.items()):
            lines.append(f"  scenario {scenario_id:<22} {digest[:12]}")
        lines.extend(model.render() for model in self.models)
        return "\n".join(lines)


def build_manifest(
    model_keys: list[str],
    *,
    models_file: Path,
    repo_root: Path,
    scenario_hashes: dict[str, str] | None = None,
) -> RunManifest:
    registry = load_registry(models_file)
    return RunManifest(
        models=[ModelManifest.of(registry.get(key)) for key in model_keys],
        git_commit=_short_commit(repo_root),
        dirty=is_dirty(repo_root),
        provenance_known=provenance_is_known(repo_root),
        scenario_hashes=scenario_hashes or {},
    )


def _short_commit(repo_root: Path) -> str:
    from evalharness.loader import git_commit

    return git_commit(repo_root)


def verify_manifest(
    manifest: RunManifest,
    *,
    models_file: Path,
    allow_dirty: bool = False,
) -> None:
    """Re-resolve the registry and refuse to launch on any disagreement.

    Deliberately re-reads ``models.yaml`` rather than trusting the manifest it
    was built from: the point is to catch a file that no longer says what the
    caller believes, not to confirm the manifest agrees with itself.
    """
    if not manifest.provenance_known and not allow_dirty:
        # Fail closed. "unknown" is not a commit, and a tree whose state cannot
        # be read is not known to be clean -- is_dirty answering False for a
        # container with no git is exactly how this guard was silently disabled
        # on the API path for every comparison run (DECISIONS D41).
        raise ManifestError(
            "refusing to launch a comparison run whose commit cannot be established: "
            "no build-time stamp and no readable git checkout, so the run would record "
            f"{manifest.git_commit!r} and the dirty-tree guard could not have fired. "
            "Rebuild the image with the GIT_COMMIT build arg, or pass --allow-dirty "
            "for a throwaway run."
        )
    if manifest.dirty and not allow_dirty:
        raise ManifestError(
            "refusing to launch a comparison run from a dirty working tree: the run would "
            f"record commit {manifest.git_commit} while the code that ran differs from it. "
            "Commit the changes, or pass --allow-dirty for a throwaway run."
        )

    registry = load_registry(models_file)
    problems: list[str] = []
    for model in manifest.models:
        try:
            current = ModelManifest.of(registry.get(model.key))
        except KeyError as exc:
            problems.append(f"{model.key}: no longer in the registry ({exc})")
            continue
        if current != model:
            for field_name in (
                "litellm_model",
                "params",
                "reasoning_effort",
                "rpm",
                "pinned_provider",
                "allow_fallbacks",
            ):
                was, now = getattr(model, field_name), getattr(current, field_name)
                if was != now:
                    problems.append(
                        f"{model.key}.{field_name}: manifest {was!r} != registry {now!r}"
                    )

    if problems:
        raise ManifestError(
            "the params manifest does not match what config/models.yaml resolves to:\n  "
            + "\n  ".join(problems)
        )
