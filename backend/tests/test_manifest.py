"""Pre-flight guards for a comparison run (DECISIONS D30).

A run records the commit and the params it ran under so results stay traceable.
Both records have already been wrong once: a `git filter-repo` reverted an
uncommitted registry edit and the next run went out with a configuration nobody
chose (D29). These checks make that a refusal instead of a discovery.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evalharness.runner.manifest import (
    ManifestError,
    ModelManifest,
    RunManifest,
    build_manifest,
    verify_manifest,
)
from evalharness.schema.registry import ModelEntry

REGISTRY = Path(__file__).resolve().parents[2] / "config" / "models.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]


def entry(**overrides: object) -> ModelEntry:
    base: dict[str, object] = {
        "key": "m",
        "display_name": "M",
        "litellm_model": "openrouter/vendor/model",
        "params": {},
        "reasoning_effort": "medium",
        "rpm": 15,
        "provider_routing": {"order": ["vendor"], "allow_fallbacks": False},
    }
    base.update(overrides)
    return ModelEntry.model_validate(base)


def manifest_of(*entries: ModelEntry, dirty: bool = False) -> RunManifest:
    return RunManifest(
        models=[ModelManifest.of(e) for e in entries],
        git_commit="abc123" + ("-dirty" if dirty else ""),
        dirty=dirty,
    )


# --------------------------------------------------------------------------- #
# The dirty-tree guard                                                         #
# --------------------------------------------------------------------------- #


def test_a_dirty_tree_refuses_to_launch() -> None:
    with pytest.raises(ManifestError, match="dirty working tree"):
        verify_manifest(manifest_of(dirty=True), models_file=REGISTRY)


def test_the_refusal_explains_the_way_out() -> None:
    with pytest.raises(ManifestError, match="--allow-dirty"):
        verify_manifest(manifest_of(dirty=True), models_file=REGISTRY)


def test_allow_dirty_overrides_it() -> None:
    """Throwaway runs still need to be possible."""
    verify_manifest(manifest_of(dirty=True), models_file=REGISTRY, allow_dirty=True)


def test_a_clean_tree_passes() -> None:
    verify_manifest(manifest_of(dirty=False), models_file=REGISTRY)


# --------------------------------------------------------------------------- #
# The params guard                                                             #
# --------------------------------------------------------------------------- #


def test_the_shipped_registry_verifies_against_itself() -> None:
    built = build_manifest(
        ["gpt-5.6-terra", "claude-sonnet-5", "gemini-3.5-flash", "gpt-oss-120b-groq"],
        models_file=REGISTRY,
        repo_root=REPO_ROOT,
    )
    verify_manifest(built, models_file=REGISTRY, allow_dirty=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("params", {"temperature": 0}),
        ("reasoning_effort", "high"),
        ("rpm", 60),
        ("litellm_model", "openrouter/vendor/other-model"),
    ],
)
def test_any_drift_from_the_registry_is_refused(tmp_path: Path, field: str, value: object) -> None:
    """A manifest built from one registry must not launch against another."""
    registry = tmp_path / "models.yaml"
    registry.write_text(
        "models:\n"
        "  - key: m\n"
        '    display_name: "M"\n'
        '    litellm_model: "openrouter/vendor/model"\n'
        "    params: {}\n"
        "    reasoning_effort: medium\n"
        "    rpm: 15\n"
        "    provider_routing: {order: [vendor], allow_fallbacks: false}\n",
        encoding="utf-8",
    )
    stale = manifest_of(entry(**{field: value}))
    with pytest.raises(ManifestError, match="does not match"):
        verify_manifest(stale, models_file=registry)


def test_a_changed_pin_is_refused(tmp_path: Path) -> None:
    """The pin is the whole basis of a fair comparison (D20)."""
    registry = tmp_path / "models.yaml"
    registry.write_text(
        "models:\n"
        "  - key: m\n"
        '    display_name: "M"\n'
        '    litellm_model: "openrouter/vendor/model"\n'
        "    params: {}\n"
        "    reasoning_effort: medium\n"
        "    rpm: 15\n"
        "    provider_routing: {order: [somewhere-else], allow_fallbacks: false}\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="pinned_provider"):
        verify_manifest(manifest_of(entry()), models_file=registry)


def test_fallbacks_being_re_enabled_is_refused(tmp_path: Path) -> None:
    registry = tmp_path / "models.yaml"
    registry.write_text(
        "models:\n"
        "  - key: m\n"
        '    display_name: "M"\n'
        '    litellm_model: "openrouter/vendor/model"\n'
        "    params: {}\n"
        "    reasoning_effort: medium\n"
        "    rpm: 15\n"
        "    provider_routing: {order: [vendor], allow_fallbacks: true}\n",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="allow_fallbacks"):
        verify_manifest(manifest_of(entry()), models_file=registry)


def test_a_model_removed_from_the_registry_is_refused(tmp_path: Path) -> None:
    registry = tmp_path / "models.yaml"
    registry.write_text(
        "models:\n"
        "  - key: someone-else\n"
        '    display_name: "Other"\n'
        '    litellm_model: "openrouter/vendor/other"\n',
        encoding="utf-8",
    )
    with pytest.raises(ManifestError, match="no longer in the registry"):
        verify_manifest(manifest_of(entry()), models_file=registry)


# --------------------------------------------------------------------------- #
# What the manifest shows                                                      #
# --------------------------------------------------------------------------- #


def test_the_manifest_prints_what_will_actually_be_sent() -> None:
    rendered = manifest_of(entry()).render()
    assert "pin=vendor" in rendered
    assert "fallbacks=False" in rendered
    assert "rpm=15" in rendered
    assert "reasoning=medium" in rendered


def test_a_dirty_manifest_says_so_on_its_face() -> None:
    assert "[DIRTY]" in manifest_of(dirty=True).render()
    assert "[DIRTY]" not in manifest_of(dirty=False).render()


def test_scenario_hashes_appear_so_the_definition_is_recorded() -> None:
    manifest = RunManifest(
        models=[ModelManifest.of(entry())],
        git_commit="abc123",
        dirty=False,
        scenario_hashes={"travel-booking": "115ed21892d3cc0b"},
    )
    rendered = manifest.render()
    assert "travel-booking" in rendered
    assert "115ed21892d3" in rendered
