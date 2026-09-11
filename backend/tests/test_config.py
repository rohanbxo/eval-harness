"""Settings and the `.env` bridge (SPEC 7, 11).

`export_dotenv` exists because provider SDKs read `os.environ` directly and
never see pydantic's view of `.env`. Without it the documented local flow — put
a key in `.env`, then `evalharness run` — fails to authenticate, even though the
same file works under docker compose.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from evalharness.config import export_dotenv, get_settings


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_values_reach_the_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXAMPLE_PROVIDER_KEY", raising=False)
    env = write_env(tmp_path, "EXAMPLE_PROVIDER_KEY=abc123\n")

    exported = export_dotenv(env)

    assert "EXAMPLE_PROVIDER_KEY" in exported
    assert os.environ["EXAMPLE_PROVIDER_KEY"] == "abc123"


def test_an_existing_environment_variable_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly exported key must beat whatever the file happens to hold."""
    monkeypatch.setenv("EXAMPLE_PROVIDER_KEY", "from-the-shell")
    env = write_env(tmp_path, "EXAMPLE_PROVIDER_KEY=from-the-file\n")

    exported = export_dotenv(env)

    assert "EXAMPLE_PROVIDER_KEY" not in exported
    assert os.environ["EXAMPLE_PROVIDER_KEY"] == "from-the-shell"


def test_blank_placeholders_are_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` ships every provider key empty; exporting those as empty
    strings only risks an SDK mistaking one for a real credential."""
    monkeypatch.delenv("EXAMPLE_PROVIDER_KEY", raising=False)
    monkeypatch.delenv("EXAMPLE_OTHER_KEY", raising=False)
    env = write_env(tmp_path, "EXAMPLE_PROVIDER_KEY=\nEXAMPLE_OTHER_KEY=set\n")

    exported = export_dotenv(env)

    assert exported == ["EXAMPLE_OTHER_KEY"]
    assert "EXAMPLE_PROVIDER_KEY" not in os.environ


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """CI runs with no `.env` at all; that must stay a supported setup."""
    assert export_dotenv(tmp_path / "absent.env") == []


def test_only_names_are_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The return value is logged by callers, so it must never carry a secret."""
    monkeypatch.delenv("EXAMPLE_PROVIDER_KEY", raising=False)
    env = write_env(tmp_path, "EXAMPLE_PROVIDER_KEY=super-secret-value\n")

    exported = export_dotenv(env)

    assert exported == ["EXAMPLE_PROVIDER_KEY"]
    assert "super-secret-value" not in "".join(exported)


def test_comments_and_blank_lines_are_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXAMPLE_PROVIDER_KEY", raising=False)
    env = write_env(
        tmp_path,
        "# a comment\n\nEXAMPLE_PROVIDER_KEY=value\n\n# trailing comment\n",
    )

    assert export_dotenv(env) == ["EXAMPLE_PROVIDER_KEY"]
    assert os.environ["EXAMPLE_PROVIDER_KEY"] == "value"


# --------------------------------------------------------------------------- #
# Settings defaults                                                            #
# --------------------------------------------------------------------------- #


def test_defaults_resolve_to_the_repo_checkout() -> None:
    """`validate` and `run --no-db` must work with no containers and no `.env`."""
    settings = get_settings()
    assert settings.scenarios_dir.name == "scenarios"
    assert settings.models_file.name == "models.yaml"


def test_judging_is_off_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVALHARNESS_ENABLE_JUDGE", raising=False)
    get_settings.cache_clear()
    assert get_settings().enable_judge is False
