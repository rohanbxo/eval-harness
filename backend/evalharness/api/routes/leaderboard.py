"""Leaderboard and run comparison (SPEC 9.2).

Both views exist to answer "is model A better than model B here", so both are
strict about only ever comparing like with like: a cell is built from a single
run, and a run's numbers are only aggregated alongside another's when the
scenario ``config_hash`` behind them matches. Where it does not, the response
says so and the UI shows a "config changed" badge instead of a misleading delta.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status

from evalharness.api.deps import SessionDep, get_registry, get_scenarios
from evalharness.api.routes.serializers import parse_summary, run_to_api
from evalharness.api.schemas import (
    AssertionFlip,
    CompareResponse,
    LeaderboardCell,
    LeaderboardResponse,
    LeaderboardRow,
    LeaderboardScenario,
    RunSummary,
    ScenarioDiff,
    ScenarioStats,
)
from evalharness.db import models, repository
from evalharness.grader.scoring import intervals_overlap, wilson_interval
from evalharness.schema.enums import Axis, RunStatus, Severity

router = APIRouter(tags=["leaderboard"])

#: Cap on how many completed runs the leaderboard scans. The view only keeps the
#: latest run per (model, scenario, config_hash), so older runs cannot change it.
_MAX_RUNS_SCANNED = 500


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _mean_axis_scores(sources: list[dict[str, float]]) -> dict[str, float]:
    """Mean per axis across sources, skipping axes a source did not measure."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for source in sources:
        for axis, score in source.items():
            buckets[axis].append(float(score))
    return {axis: sum(scores) / len(scores) for axis, scores in sorted(buckets.items())}


def _total_cost(values: list[float | None]) -> float | None:
    """Sum of costs, or None when any component is unknown -- never a guess (SPEC 6.3)."""
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _scenario_stats(summary: RunSummary | None, scenario_id: str) -> ScenarioStats | None:
    if summary is None:
        return None
    return next((s for s in summary.per_scenario if s.scenario_id == scenario_id), None)


@router.get(
    "/leaderboard",
    response_model=LeaderboardResponse,
    operation_id="getLeaderboard",
    summary="Per model x scenario: pass@1, pass^k, axis scores, cost and latency",
)
async def get_leaderboard(session: SessionDep) -> LeaderboardResponse:
    runs, _ = await repository.list_runs(
        session, status=RunStatus.COMPLETED, limit=_MAX_RUNS_SCANNED, offset=0
    )

    scenarios_on_disk = get_scenarios()
    current_hashes: dict[str, str | None] = {
        scenario_id: (entry.loaded.config_hash if entry.loaded is not None else None)
        for scenario_id, entry in scenarios_on_disk.items()
    }

    # list_runs returns newest first, so the first run seen for a key wins.
    latest: dict[tuple[str, str], LeaderboardCell] = {}
    seen_scenarios: set[str] = set()

    for run in runs:
        summary = parse_summary(run.summary)
        hashes = dict(run.config_hashes or {})
        for scenario_id in run.scenario_ids:
            seen_scenarios.add(scenario_id)
            key = (run.model_key, scenario_id)
            if key in latest:
                continue
            stats = _scenario_stats(summary, scenario_id)
            if stats is None:
                continue
            # A scenario whose every attempt errored is NOT the same as one that
            # was never run: the first is a failure to collect data, the second
            # is an absence of it. Skipping the former made them identical in the
            # UI, both showing "no run" (DECISIONS D26).
            if stats.completed == 0 and stats.attempts == 0:
                continue
            config_hash = stats.config_hash or hashes.get(scenario_id, "")
            current = current_hashes.get(scenario_id)
            latest[key] = LeaderboardCell(
                model_key=run.model_key,
                scenario_id=scenario_id,
                run_id=run.id,
                created_at=run.created_at,
                k=run.k,
                config_hash=config_hash,
                config_changed=bool(current and config_hash and current != config_hash),
                attempts=stats.attempts,
                incomplete=not stats.complete,
                coverage=stats.coverage,
                pass_at_1=stats.pass_at_1,
                passed=stats.passed,
                graded=stats.completed,
                pass_at_1_low=wilson_interval(stats.passed, stats.completed)[0],
                pass_at_1_high=wilson_interval(stats.passed, stats.completed)[1],
                pass_hat_k=stats.pass_hat_k,
                axis_scores=stats.axis_scores,
                cost_usd=stats.cost_usd,
                latency_p50_ms=stats.latency_p50_ms,
                latency_p95_ms=stats.latency_p95_ms,
            )

    registry = get_registry()
    by_model: dict[str, list[LeaderboardCell]] = defaultdict(list)
    for (model_key, _), cell in latest.items():
        by_model[model_key].append(cell)

    rows: list[LeaderboardRow] = []
    for model_key, cells in by_model.items():
        ordered = sorted(cells, key=lambda c: c.scenario_id)
        try:
            entry = registry.get(model_key)
            display_name, litellm_model = entry.display_name, entry.litellm_model
        except KeyError:
            # A run whose model has since been removed from the registry still has
            # results worth showing; fall back to the key itself.
            display_name, litellm_model = model_key, ""
        row_passed = sum(c.passed for c in ordered)
        row_graded = sum(c.graded for c in ordered)
        low, high = wilson_interval(row_passed, row_graded)
        rows.append(
            LeaderboardRow(
                model_key=model_key,
                passed=row_passed,
                graded=row_graded,
                pass_at_1_low=low,
                pass_at_1_high=high,
                display_name=display_name,
                litellm_model=litellm_model,
                scenarios_covered=len(ordered),
                pass_at_1=sum(c.pass_at_1 for c in ordered) / len(ordered),
                pass_hat_k=(
                    sum(c.pass_hat_k for c in scored) / len(scored)
                    if (scored := [c for c in ordered if not c.incomplete])
                    else 0.0
                ),
                axis_scores=_mean_axis_scores([c.axis_scores for c in ordered]),
                cost_usd=_total_cost([c.cost_usd for c in ordered]),
                latency_p50_ms=_mean([c.latency_p50_ms for c in ordered if c.latency_p50_ms]),
                latency_p95_ms=_mean([c.latency_p95_ms for c in ordered if c.latency_p95_ms]),
                has_config_drift=any(c.config_changed for c in ordered),
                has_incomplete=any(c.incomplete for c in ordered),
                cells=ordered,
            )
        )

    rows.sort(key=lambda r: (-r.pass_hat_k, -r.pass_at_1, r.model_key))

    # Flag every row whose interval overlaps the leader's: with runs this small a
    # visible gap in pass@1 is often not established at 95%, and the leaderboard
    # should say so rather than let the ordering imply more than it shows.
    if rows:
        leader = (rows[0].pass_at_1_low, rows[0].pass_at_1_high)
        for row in rows:
            row.not_significant_vs_leader = intervals_overlap(
                leader, (row.pass_at_1_low, row.pass_at_1_high)
            )

    def title_of(scenario_id: str) -> str:
        entry = scenarios_on_disk.get(scenario_id)
        if entry is None or entry.loaded is None:
            return ""  # ran against a scenario that has since been removed or broke
        return entry.loaded.scenario.title

    scenario_ids = sorted(seen_scenarios | set(scenarios_on_disk))
    return LeaderboardResponse(
        generated_at=datetime.now(UTC),
        scenarios=[
            LeaderboardScenario(
                scenario_id=scenario_id,
                title=title_of(scenario_id),
                current_config_hash=current_hashes.get(scenario_id),
            )
            for scenario_id in scenario_ids
        ],
        rows=rows,
    )


def _delta(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else b - a


def _flip_direction(
    rate_a: float | None, rate_b: float | None
) -> Literal["fixed", "broken", "improved", "regressed", "added", "removed"] | None:
    """Classify a change in an assertion's pass rate between two runs."""
    if rate_a is None:
        return "added" if rate_b is not None else None
    if rate_b is None:
        return "removed"
    if rate_a == rate_b:
        return None
    # A clean 0 -> 1 (or 1 -> 0) is a real fix or a real break; anything else is a
    # shift in flakiness, which is worth showing but is a weaker claim.
    if rate_a == 0.0 and rate_b == 1.0:
        return "fixed"
    if rate_a == 1.0 and rate_b == 0.0:
        return "broken"
    return "improved" if rate_b > rate_a else "regressed"


async def _assertion_pass_rates(
    session: SessionDep, run_id: str
) -> dict[tuple[str, str], tuple[float, str, str, str]]:
    """``(scenario_id, assertion_id) -> (pass_rate, type, axis, severity)``."""
    rows = await repository.run_assertion_rows(session, run_id)
    totals: dict[tuple[str, str], list[bool]] = defaultdict(list)
    meta: dict[tuple[str, str], tuple[str, str, str]] = {}
    for scenario_id, assertion_id, type_, axis, severity, passed in rows:
        key = (scenario_id, assertion_id)
        totals[key].append(passed)
        meta[key] = (type_, axis, severity)
    return {
        key: (sum(values) / len(values), *meta[key]) for key, values in totals.items() if values
    }


def _as_axis(value: str) -> Axis | None:
    try:
        return Axis(value)
    except ValueError:  # pragma: no cover - only if an axis is renamed
        return None


def _as_severity(value: str) -> Severity | None:
    try:
        return Severity(value)
    except ValueError:  # pragma: no cover - only if a severity is renamed
        return None


async def _require_run(session: SessionDep, run_id: str) -> models.Run:
    run = await repository.get_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown run {run_id!r}")
    return run


@router.get(
    "/compare",
    response_model=CompareResponse,
    operation_id="compareRuns",
    summary="Side-by-side summary, per-scenario diffs and assertions that flipped",
)
async def compare_runs(
    session: SessionDep,
    run_a: str = Query(description="Baseline run id."),
    run_b: str = Query(description="Run to compare against the baseline."),
) -> CompareResponse:
    a = await _require_run(session, run_a)
    b = await _require_run(session, run_b)

    summary_a, summary_b = parse_summary(a.summary), parse_summary(b.summary)
    hashes_a, hashes_b = dict(a.config_hashes or {}), dict(b.config_hashes or {})

    diffs: list[ScenarioDiff] = []
    for scenario_id in sorted(set(a.scenario_ids) | set(b.scenario_ids)):
        stats_a = _scenario_stats(summary_a, scenario_id)
        stats_b = _scenario_stats(summary_b, scenario_id)
        hash_a = (stats_a.config_hash if stats_a else "") or hashes_a.get(scenario_id, "")
        hash_b = (stats_b.config_hash if stats_b else "") or hashes_b.get(scenario_id, "")
        axes_a = stats_a.axis_scores if stats_a else {}
        axes_b = stats_b.axis_scores if stats_b else {}
        diffs.append(
            ScenarioDiff(
                scenario_id=scenario_id,
                config_hash_a=hash_a,
                config_hash_b=hash_b,
                config_changed=bool(hash_a and hash_b and hash_a != hash_b),
                incomplete_a=bool(stats_a and not stats_a.complete),
                incomplete_b=bool(stats_b and not stats_b.complete),
                pass_at_1_a=stats_a.pass_at_1 if stats_a else None,
                pass_at_1_b=stats_b.pass_at_1 if stats_b else None,
                pass_at_1_delta=_delta(
                    stats_a.pass_at_1 if stats_a else None,
                    stats_b.pass_at_1 if stats_b else None,
                ),
                pass_hat_k_a=stats_a.pass_hat_k if stats_a else None,
                pass_hat_k_b=stats_b.pass_hat_k if stats_b else None,
                pass_hat_k_delta=_delta(
                    stats_a.pass_hat_k if stats_a else None,
                    stats_b.pass_hat_k if stats_b else None,
                ),
                axis_scores_a=axes_a,
                axis_scores_b=axes_b,
                axis_deltas={
                    axis: axes_b[axis] - axes_a[axis]
                    for axis in sorted(set(axes_a) & set(axes_b))
                    if axes_b[axis] != axes_a[axis]
                },
                cost_usd_a=stats_a.cost_usd if stats_a else None,
                cost_usd_b=stats_b.cost_usd if stats_b else None,
                cost_usd_delta=_delta(
                    stats_a.cost_usd if stats_a else None,
                    stats_b.cost_usd if stats_b else None,
                ),
                latency_p50_ms_a=stats_a.latency_p50_ms if stats_a else None,
                latency_p50_ms_b=stats_b.latency_p50_ms if stats_b else None,
                latency_p50_ms_delta=_delta(
                    stats_a.latency_p50_ms if stats_a else None,
                    stats_b.latency_p50_ms if stats_b else None,
                ),
            )
        )

    rates_a = await _assertion_pass_rates(session, run_a)
    rates_b = await _assertion_pass_rates(session, run_b)

    flips: list[AssertionFlip] = []
    for key in sorted(set(rates_a) | set(rates_b)):
        scenario_id, assertion_id = key
        entry_a, entry_b = rates_a.get(key), rates_b.get(key)
        rate_a = entry_a[0] if entry_a else None
        rate_b = entry_b[0] if entry_b else None
        direction = _flip_direction(rate_a, rate_b)
        if direction is None:
            continue
        meta = entry_b or entry_a
        flips.append(
            AssertionFlip(
                scenario_id=scenario_id,
                assertion_id=assertion_id,
                type=meta[1] if meta else "",
                axis=_as_axis(meta[2]) if meta else None,
                severity=_as_severity(meta[3]) if meta else None,
                pass_rate_a=rate_a,
                pass_rate_b=rate_b,
                direction=direction,
            )
        )

    return CompareResponse(
        run_a=run_to_api(a),
        run_b=run_to_api(b),
        scenarios=diffs,
        flipped_assertions=flips,
    )
