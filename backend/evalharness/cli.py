"""Headless CLI (SPEC 9.3).

``validate`` is the scenario test suite: it loads every scenario, then replays
each transcript through the real runner and the real grader with ``FakeModel``
and checks the verdict against what the transcript declares.

``run --no-db`` executes attempts synchronously. This module deliberately does
not import ``db``, ``api`` or ``worker``, so it works with no Postgres, no Redis
and no Celery -- that is what makes it usable in CI.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from evalharness.config import Settings, export_dotenv, get_settings
from evalharness.grader.scoring import summarize_run
from evalharness.loader import (
    LoadedScenario,
    RegistryError,
    ScenarioValidationError,
    discover_scenario_dirs,
    load_registry,
    load_scenario,
    load_transcripts,
)
from evalharness.runner import AttemptContext, FakeModel, LiteLLMProvider, Provider, run_attempt
from evalharness.schema.registry import ModelEntry
from evalharness.schema.runtime import AttemptResult
from evalharness.schema.transcript import Transcript

app = typer.Typer(
    name="evalharness",
    help="Config-driven evaluation harness for agentic LLM tool use.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


@app.callback()
def _bootstrap() -> None:
    """Make ``.env`` visible to provider SDKs before any command runs.

    LiteLLM reads credentials from ``os.environ``, which pydantic-settings does
    not populate, so without this the documented flow (put a key in ``.env``,
    then ``evalharness run``) would fail to authenticate.
    """
    export_dotenv()


OK = "[green]PASS[/green]"
BAD = "[red]FAIL[/red]"

ScenariosDirOption = Annotated[
    Path | None,
    typer.Option("--scenarios-dir", help="Override EVALHARNESS_SCENARIOS_DIR."),
]
ModelsFileOption = Annotated[
    Path | None,
    typer.Option("--models-file", help="Override EVALHARNESS_MODELS_FILE."),
]


def _settings() -> Settings:
    return get_settings()


def _scenarios_root(override: Path | None) -> Path:
    return override if override is not None else _settings().scenarios_dir


def _models_file(override: Path | None) -> Path:
    return override if override is not None else _settings().models_file


def _fail(message: str, code: int = 2) -> typer.Exit:
    err_console.print(f"[red]error:[/red] {message}")
    return typer.Exit(code)


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


@dataclass
class TranscriptCheck:
    """The verdict on replaying one transcript."""

    scenario_id: str
    name: str
    ok: bool
    detail: str


def _axis_gaps(result: AttemptResult) -> list[str]:
    return [
        f"{axis}={score:.2f}" for axis, score in sorted(result.axis_scores.items()) if score < 1
    ]


def check_transcript(loaded: LoadedScenario, name: str, transcript: Transcript) -> TranscriptCheck:
    """Replay one transcript through the real runner and grader (SPEC 6.4)."""

    def verdict(ok: bool, detail: str) -> TranscriptCheck:
        return TranscriptCheck(loaded.id, name, ok, detail)

    if transcript.scenario != loaded.id:
        return verdict(
            False, f"declares scenario {transcript.scenario!r}, but lives in {loaded.id}"
        )

    ctx = AttemptContext(
        loaded=loaded, provider=FakeModel(transcript, label=f"{loaded.id}/{name}"), repetition=0
    )
    result = asyncio.run(run_attempt(ctx))

    if result.error is not None:
        return verdict(False, f"attempt errored: {result.error}")

    failed = [r.assertion_id for r in result.assertion_results if not r.passed]
    if transcript.expect_pass:
        if not result.passed:
            reasons = "; ".join(
                f"{r.assertion_id}: {r.reason}" for r in result.assertion_results if not r.passed
            )
            return verdict(False, f"expected a pass, but failed: {reasons}")
        if transcript.expect_all_axes_full:
            gaps = _axis_gaps(result)
            if gaps:
                return verdict(False, "expected every axis at 1.0, got " + ", ".join(gaps))
        return verdict(True, f"passed, {len(result.assertion_results)} assertion(s)")

    if result.passed:
        return verdict(False, "expected a failure, but the attempt passed")
    expected = sorted(set(transcript.expect_failures))
    actual = sorted(set(failed))
    if actual != expected:
        missing = [a for a in expected if a not in actual]
        extra = [a for a in actual if a not in expected]
        parts = []
        if missing:
            parts.append(f"expected to fail but passed: {', '.join(missing)}")
        if extra:
            parts.append(f"failed unexpectedly: {', '.join(extra)}")
        return verdict(False, "; ".join(parts))
    return verdict(True, f"failed on exactly {', '.join(actual)}")


def _load_selected(root: Path, scenario_id: str | None) -> list[LoadedScenario]:
    directories = discover_scenario_dirs(root)
    if scenario_id is not None:
        directories = [d for d in directories if d.name == scenario_id]
        if not directories:
            raise ScenarioValidationError(root / scenario_id, "no such scenario")
    return [load_scenario(d) for d in directories]


@app.command()
def validate(
    scenario: Annotated[
        str | None, typer.Option("--scenario", help="Validate only this scenario id.")
    ] = None,
    scenarios_dir: ScenariosDirOption = None,
) -> None:
    """Load and schema-check scenarios, then run their transcript tests."""
    root = _scenarios_root(scenarios_dir)
    if not root.is_dir():
        console.print(f"[yellow]no scenarios directory at {root}[/yellow] -- nothing to validate")
        raise typer.Exit(0)

    try:
        loaded_scenarios = _load_selected(root, scenario)
    except ScenarioValidationError as exc:
        raise _fail(str(exc), code=1) from exc

    if not loaded_scenarios:
        console.print(
            f"[yellow]no scenarios found in {root}[/yellow] -- "
            "add a directory with a scenario.yaml to validate it"
        )
        raise typer.Exit(0)

    table = Table(title="evalharness validate", show_lines=False)
    table.add_column("scenario")
    table.add_column("transcript")
    table.add_column("result")
    table.add_column("detail", overflow="fold")

    failures = 0
    for loaded in loaded_scenarios:
        table.add_row(
            loaded.id,
            "[dim]schema[/dim]",
            OK,
            f"{len(loaded.scenario.turns)} turn(s), {len(loaded.tools)} tool(s), "
            f"hash {loaded.config_hash[:12]}",
        )
        transcripts = load_transcripts(loaded.directory)
        if not transcripts:
            failures += 1
            table.add_row(
                loaded.id,
                "[dim]transcripts[/dim]",
                BAD,
                "no transcripts/ files; a scenario without transcript tests is not done (SPEC 6.4)",
            )
            continue
        for name in sorted(transcripts):
            check = check_transcript(loaded, name, transcripts[name])
            failures += 0 if check.ok else 1
            table.add_row(loaded.id, name, OK if check.ok else BAD, check.detail)

    console.print(table)
    if failures:
        err_console.print(f"[red]{failures} check(s) failed[/red]")
        raise typer.Exit(1)
    console.print("[green]all checks passed[/green]")


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _provider_for(entry: ModelEntry, loaded: LoadedScenario) -> Provider:
    """FakeModel replays the scenario's golden transcript; anything else is LiteLLM."""
    if entry.litellm_model.startswith("fake/"):
        transcripts = load_transcripts(loaded.directory)
        golden = transcripts.get("golden")
        if golden is None:
            raise ScenarioValidationError(
                loaded.directory / "transcripts",
                "the fake model needs transcripts/golden.yaml to replay",
            )
        return FakeModel(golden, label=f"{loaded.id}/golden")
    return LiteLLMProvider(entry)


async def _run_attempts(
    entry: ModelEntry, scenarios: list[LoadedScenario], k: int
) -> list[AttemptResult]:
    results: list[AttemptResult] = []
    for loaded in scenarios:
        for repetition in range(k):
            provider = _provider_for(entry, loaded)
            result = await run_attempt(
                AttemptContext(loaded=loaded, provider=provider, repetition=repetition)
            )
            results.append(result)
            mark = "[green]pass[/green]" if result.passed else "[red]fail[/red]"
            note = f" [red]({result.error})[/red]" if result.error else ""
            console.print(f"  {loaded.id} #{repetition}: {mark}{note}")
    return results


def _results_document(
    entry: ModelEntry,
    scenarios: list[LoadedScenario],
    k: int,
    results: list[AttemptResult],
    *,
    include_events: bool,
) -> dict[str, Any]:
    return {
        "model_key": entry.key,
        "litellm_model": entry.litellm_model,
        "k": k,
        "scenarios": {s.id: s.config_hash for s in scenarios},
        "summary": summarize_run(results, k).to_dict(),
        "attempts": [
            r.model_dump(mode="json", exclude={"events"} if not include_events else set())
            for r in results
        ],
    }


@app.command()
def run(
    model: Annotated[str, typer.Option("--model", help="Model key from the registry.")],
    scenarios: Annotated[
        str, typer.Option("--scenarios", help="'all' or a comma-separated list of ids.")
    ] = "all",
    k: Annotated[int, typer.Option("--k", min=1, help="Repetitions per scenario.")] = 1,
    no_db: Annotated[
        bool, typer.Option("--no-db", help="Run synchronously, without Postgres or Celery.")
    ] = False,
    out: Annotated[Path | None, typer.Option("--out", help="Write the results JSON here.")] = None,
    include_events: Annotated[
        bool, typer.Option("--include-events", help="Include the full event log in --out.")
    ] = False,
    scenarios_dir: ScenariosDirOption = None,
    models_file: ModelsFileOption = None,
) -> None:
    """Run scenarios against a model."""
    if not no_db:
        raise _fail(
            "database-backed runs are launched through the API (POST /api/runs). "
            "Use --no-db for a local synchronous run."
        )

    try:
        registry = load_registry(_models_file(models_file))
    except RegistryError as exc:
        raise _fail(str(exc)) from exc
    try:
        entry = registry.get(model)
    except KeyError as exc:
        raise _fail(str(exc.args[0])) from exc
    if not entry.supports_tool_calling:
        raise _fail(f"model {entry.key!r} does not support tool calling; runs are skipped")
    if entry.api_key_env and not os.environ.get(entry.api_key_env):
        raise _fail(f"{entry.api_key_env} is not set, which model {entry.key!r} needs")

    root = _scenarios_root(scenarios_dir)
    try:
        available = _load_selected(root, None)
    except ScenarioValidationError as exc:
        raise _fail(str(exc), code=1) from exc
    if not available:
        raise _fail(f"no scenarios found in {root}", code=1)

    if scenarios.strip() == "all":
        selected = available
    else:
        wanted = [s.strip() for s in scenarios.split(",") if s.strip()]
        by_id = {s.id: s for s in available}
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            raise _fail(
                f"unknown scenario(s): {', '.join(unknown)}; available: {', '.join(sorted(by_id))}"
            )
        selected = [by_id[w] for w in wanted]

    console.print(f"running [bold]{entry.key}[/bold] over {len(selected)} scenario(s), k={k}")
    try:
        results = asyncio.run(_run_attempts(entry, selected, k))
    except ScenarioValidationError as exc:
        raise _fail(str(exc), code=1) from exc

    summary = summarize_run(results, k)
    table = Table(title=f"run summary: {entry.key}")
    table.add_column("metric")
    table.add_column("value")
    table.add_row("attempts", str(summary.attempts))
    table.add_row(
        "graded",
        f"{summary.graded_attempts}/{summary.attempts} ({summary.coverage:.0%} coverage)",
    )
    if summary.errored_attempts:
        table.add_row("errored", f"[yellow]{summary.errored_attempts}[/yellow]")
    table.add_row("pass@1", f"{summary.pass_at_1:.2f}" + ("*" if summary.incomplete else ""))
    table.add_row(
        "pass^k",
        f"{summary.pass_hat_k:.2f} "
        f"(over {summary.scenarios_scored}/{summary.scenarios_total} scenarios)",
    )
    table.add_row("cost_usd", "unknown" if summary.cost_usd is None else f"{summary.cost_usd:.6f}")
    table.add_row("tokens", f"{summary.input_tokens} in / {summary.output_tokens} out")
    for axis, score in summary.axis_scores.items():
        table.add_row(f"axis:{axis}", f"{score:.2f}")
    console.print(table)

    if summary.incomplete:
        # Say it plainly: a partial run's rates describe the attempts that ran,
        # and reading them as the model's score would be a mistake.
        console.print(
            f"[yellow]INCOMPLETE[/yellow]: {summary.errored_attempts} of {summary.attempts} "
            "attempt(s) never produced a verdict, so the rates above (*) cover only the "
            f"{summary.graded_attempts} that did."
        )

    if out is not None:
        document = _results_document(entry, selected, k, results, include_events=include_events)
        out.write_text(json.dumps(document, indent=2), encoding="utf-8")
        console.print(f"wrote {out}")

    # A partial run is not a pass, even if everything that ran passed.
    if summary.incomplete or summary.passed_attempts != summary.graded_attempts:
        raise typer.Exit(1)


# --------------------------------------------------------------------------
# list-models
# --------------------------------------------------------------------------


@app.command("list-models")
def list_models(models_file: ModelsFileOption = None) -> None:
    """List the model registry, flagging models whose API key is missing."""
    try:
        registry = load_registry(_models_file(models_file))
    except RegistryError as exc:
        raise _fail(str(exc)) from exc

    table = Table(title="models")
    table.add_column("key")
    table.add_column("display name")
    table.add_column("litellm model")
    table.add_column("parallel")
    table.add_column("api key")
    for entry in registry.models:
        if not entry.api_key_env:
            key_state = "[dim]not needed[/dim]"
        elif os.environ.get(entry.api_key_env):
            key_state = f"[green]{entry.api_key_env}[/green]"
        else:
            key_state = f"[red]missing {entry.api_key_env}[/red]"
        table.add_row(
            entry.key,
            entry.display_name,
            entry.litellm_model,
            "yes" if entry.supports_parallel_tool_calls else "no",
            key_state,
        )
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
