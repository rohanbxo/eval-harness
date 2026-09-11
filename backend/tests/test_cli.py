"""Tests for the headless CLI (SPEC 9.3).

`run --no-db` is what CI and a quick local check use, so it must work with no
Postgres, no Celery, no broker and no API key. These tests drive the real
commands end to end against the repo's real scenarios, using the `fake` model.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalharness.cli import app

REPO_ROOT = Path(__file__).resolve().parents[2]
SCENARIOS_DIR = REPO_ROOT / "scenarios"
MODELS_FILE = REPO_ROOT / "config" / "models.yaml"

runner = CliRunner()

_ANSI = re.compile(r"\[[0-9;]*m")

_PLAIN_TERMINAL = {"TERM": "dumb", "NO_COLOR": "1"}


def invoke(*args: str) -> tuple[int, str]:
    """Run the CLI without colour and return (exit code, output)."""
    result = runner.invoke(app, list(args), catch_exceptions=False, env=_PLAIN_TERMINAL)
    return result.exit_code, _ANSI.sub("", result.output)


def flat(output: str) -> str:
    """Undo Rich's line wrapping.

    Rich fixes its width when the console is built, so a long path or phrase is
    wrapped before these tests can widen anything. Joining the lines back up
    keeps the assertions about what was said, not where it broke.
    """
    return output.replace("\n", "")


def with_config(*args: str) -> tuple[int, str]:
    return invoke(*args, "--scenarios-dir", str(SCENARIOS_DIR), "--models-file", str(MODELS_FILE))


# --------------------------------------------------------------------------- #
# list-models                                                                  #
# --------------------------------------------------------------------------- #


def test_list_models_shows_the_registry() -> None:
    code, output = invoke("list-models", "--models-file", str(MODELS_FILE))
    assert code == 0
    assert "fake" in output
    assert "opus-5" in output


def test_list_models_reports_a_missing_registry_clearly() -> None:
    code, output = invoke("list-models", "--models-file", str(REPO_ROOT / "nope.yaml"))
    assert code != 0
    assert "not found" in output


def test_list_models_reports_a_malformed_registry(tmp_path: Path) -> None:
    bad = tmp_path / "models.yaml"
    bad.write_text("models: [{key: 'Not A Valid Key!'}]\n", encoding="utf-8")
    code, _ = invoke("list-models", "--models-file", str(bad))
    assert code != 0


# --------------------------------------------------------------------------- #
# validate                                                                     #
# --------------------------------------------------------------------------- #


def test_validate_passes_for_every_shipped_scenario() -> None:
    """The whole point of `validate`: goldens pass, fail_* fail on exactly their ids."""
    code, output = invoke("validate", "--scenarios-dir", str(SCENARIOS_DIR))
    assert code == 0, output
    assert "all checks passed" in output


@pytest.mark.parametrize(
    "scenario_id",
    [
        "travel-booking",
        "refund-policy",
        "data-analyst",
        "meeting-scheduler",
        "research-injection",
    ],
)
def test_validate_one_scenario_at_a_time(scenario_id: str) -> None:
    code, output = invoke(
        "validate", "--scenario", scenario_id, "--scenarios-dir", str(SCENARIOS_DIR)
    )
    assert code == 0, output
    assert scenario_id in output


def test_validate_rejects_an_unknown_scenario() -> None:
    code, output = invoke(
        "validate", "--scenario", "no-such-scenario", "--scenarios-dir", str(SCENARIOS_DIR)
    )
    assert code != 0
    assert "no-such-scenario" in output


def test_validate_on_an_empty_directory_says_so_instead_of_crashing(tmp_path: Path) -> None:
    code, output = invoke("validate", "--scenarios-dir", str(tmp_path))
    assert code == 0
    assert "no scenarios" in output.lower()


def test_validate_on_a_missing_directory_says_so(tmp_path: Path) -> None:
    code, output = invoke("validate", "--scenarios-dir", str(tmp_path / "absent"))
    assert code == 0
    assert "nothing to validate" in flat(output)


def test_validate_reports_a_broken_scenario_with_file_and_reason(tmp_path: Path) -> None:
    """A validation error has to name the file, the path and the reason (Phase 1)."""
    broken = tmp_path / "broken-scenario"
    broken.mkdir()
    (broken / "scenario.yaml").write_text(
        "id: broken-scenario\n"
        "version: 1\n"
        "title: Broken\n"
        "clock: '2026-03-02T09:00:00+04:00'\n"
        "system_prompt: hi\n"
        "surprise_key: nope\n"
        "turns:\n"
        "  - user: hello\n",
        encoding="utf-8",
    )
    code, output = invoke("validate", "--scenarios-dir", str(tmp_path))
    assert code != 0
    assert "scenario.yaml" in flat(output)
    assert "surprise_key" in flat(output)


# --------------------------------------------------------------------------- #
# run --no-db                                                                  #
# --------------------------------------------------------------------------- #


def test_run_requires_no_db() -> None:
    """Database-backed runs go through the API; the CLI says so rather than half-working."""
    code, output = with_config("run", "--model", "fake", "--scenarios", "travel-booking")
    assert code != 0
    assert "--no-db" in output


def test_run_rejects_an_unknown_model() -> None:
    code, output = with_config(
        "run", "--model", "no-such-model", "--scenarios", "travel-booking", "--no-db"
    )
    assert code != 0
    assert "no-such-model" in output


def test_run_rejects_an_unknown_scenario() -> None:
    code, output = with_config("run", "--model", "fake", "--scenarios", "nope", "--no-db")
    assert code != 0
    assert "nope" in output


def test_run_one_scenario_passes_on_the_golden_transcript() -> None:
    code, output = with_config("run", "--model", "fake", "--scenarios", "travel-booking", "--no-db")
    assert code == 0, output
    assert "pass" in output


def test_run_all_scenarios_scores_every_axis(tmp_path: Path) -> None:
    out = tmp_path / "results.json"
    code, output = with_config(
        "run", "--model", "fake", "--scenarios", "all", "--k", "1", "--no-db", "--out", str(out)
    )
    assert code == 0, output

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["model_key"] == "fake"
    assert document["k"] == 1
    assert len(document["attempts"]) == 5
    assert set(document["scenarios"]) == {
        "travel-booking",
        "refund-policy",
        "data-analyst",
        "meeting-scheduler",
        "research-injection",
    }
    # Every scenario ships a config hash so results stay comparable (SPEC 8.2).
    assert all(document["scenarios"].values())

    summary = document["summary"]
    assert summary["pass_at_1"] == 1.0
    assert summary["pass_hat_k"] == 1.0
    # The goldens between them exercise all eight axes at full marks.
    assert set(summary["axis_scores"]) == {
        "selection",
        "arguments",
        "ordering",
        "restraint",
        "recovery",
        "state",
        "clarification",
        "safety",
    }
    assert all(score == 1.0 for score in summary["axis_scores"].values())


def test_run_repeats_k_times() -> None:
    code, output = with_config(
        "run", "--model", "fake", "--scenarios", "travel-booking", "--k", "3", "--no-db"
    )
    assert code == 0, output
    # One line per repetition, 0-indexed.
    for repetition in range(3):
        assert f"travel-booking #{repetition}" in output


def test_run_accepts_a_comma_separated_scenario_list(tmp_path: Path) -> None:
    out = tmp_path / "results.json"
    code, _ = with_config(
        "run",
        "--model",
        "fake",
        "--scenarios",
        "travel-booking,refund-policy",
        "--no-db",
        "--out",
        str(out),
    )
    assert code == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert set(document["scenarios"]) == {"travel-booking", "refund-policy"}


def test_results_exclude_events_unless_asked(tmp_path: Path) -> None:
    """The event log is large; --out stays readable until you ask for it."""
    lean = tmp_path / "lean.json"
    with_config(
        "run", "--model", "fake", "--scenarios", "travel-booking", "--no-db", "--out", str(lean)
    )
    document = json.loads(lean.read_text(encoding="utf-8"))
    assert "events" not in document["attempts"][0]

    full = tmp_path / "full.json"
    with_config(
        "run",
        "--model",
        "fake",
        "--scenarios",
        "travel-booking",
        "--no-db",
        "--out",
        str(full),
        "--include-events",
    )
    document = json.loads(full.read_text(encoding="utf-8"))
    events = document["attempts"][0]["events"]
    assert events
    assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)


def test_run_reports_the_assertion_results_in_the_document(tmp_path: Path) -> None:
    out = tmp_path / "results.json"
    with_config(
        "run", "--model", "fake", "--scenarios", "research-injection", "--no-db", "--out", str(out)
    )
    attempt = json.loads(out.read_text(encoding="utf-8"))["attempts"][0]
    ids = {row["assertion_id"] for row in attempt["assertion_results"]}
    assert "t1-no-email" in ids
    assert attempt["passed"] is True
    assert attempt["critical_failure"] is False


def test_run_needs_a_golden_transcript_for_the_fake_model(tmp_path: Path) -> None:
    """FakeModel replays `golden`; a scenario without one must say that plainly."""
    source = SCENARIOS_DIR / "travel-booking"
    target = tmp_path / "travel-booking"
    target.mkdir()
    for name in ("scenario.yaml", "tools.json"):
        (target / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    (target / "fixtures").mkdir()
    for fixture in (source / "fixtures").glob("*.json"):
        (target / "fixtures" / fixture.name).write_text(
            fixture.read_text(encoding="utf-8"), encoding="utf-8"
        )

    code, output = invoke(
        "run",
        "--model",
        "fake",
        "--scenarios",
        "travel-booking",
        "--no-db",
        "--scenarios-dir",
        str(tmp_path),
        "--models-file",
        str(MODELS_FILE),
    )
    assert code != 0
    assert "golden" in output


# --------------------------------------------------------------------------- #
# Import hygiene                                                               #
# --------------------------------------------------------------------------- #


def test_cli_import_does_not_pull_in_the_database_stack() -> None:
    """`--no-db` has to work with no infrastructure, so the CLI must not import it.

    Run in a subprocess: another test may already have imported these modules.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import evalharness.cli; "
        "bad = [m for m in ('sqlalchemy', 'celery', 'litellm', 'fastapi') if m in sys.modules]; "
        "print(','.join(bad))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert completed.stdout.strip() == "", (
        f"importing the CLI pulled in heavy modules: {completed.stdout.strip()}"
    )
