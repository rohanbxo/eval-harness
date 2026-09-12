"""Database rows -> API response models.

Kept in one place so every route returns the same shapes, which is what the
generated TypeScript client depends on.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from evalharness.api import schemas
from evalharness.api.deps import ScenarioEntry, api_key_env_for, api_key_present
from evalharness.db import models
from evalharness.schema.enums import Axis, EventType, Severity
from evalharness.schema.registry import ModelEntry

LOGGER = logging.getLogger(__name__)


def parse_summary(raw: dict[str, Any] | None) -> schemas.RunSummary | None:
    """``runs.summary`` is jsonb; a half-written or legacy blob degrades to None."""
    if not raw:
        return None
    try:
        return schemas.RunSummary.model_validate(raw)
    except ValidationError:
        LOGGER.debug("run summary is not a RunSummary payload; reporting none")
        return None


def run_to_api(run: models.Run, counts: dict[str, int] | None = None) -> schemas.Run:
    return schemas.Run(
        id=run.id,
        created_at=run.created_at,
        status=run.status,
        model_key=run.model_key,
        litellm_model=run.litellm_model,
        params=dict(run.params or {}),
        k=run.k,
        scenario_ids=list(run.scenario_ids or []),
        config_hashes=dict(run.config_hashes or {}),
        git_commit=run.git_commit,
        harness_version=run.harness_version,
        summary=parse_summary(run.summary),
        attempt_counts=counts or {},
    )


def attempt_to_api(attempt: models.Attempt) -> schemas.AttemptSummary:
    return schemas.AttemptSummary(
        id=attempt.id,
        run_id=attempt.run_id,
        scenario_id=attempt.scenario_id,
        repetition=attempt.repetition,
        status=attempt.status,
        passed=attempt.passed,
        critical_failure=attempt.critical_failure,
        axis_scores=dict(attempt.axis_scores or {}),
        cost_usd=attempt.cost_usd,
        input_tokens=attempt.input_tokens,
        output_tokens=attempt.output_tokens,
        duration_ms=attempt.duration_ms,
        error=attempt.error,
        exposed=attempt.exposed,
    )


def run_detail(
    run: models.Run,
    attempts: list[models.Attempt],
    counts: dict[str, int] | None = None,
) -> schemas.RunDetail:
    base = run_to_api(run, counts)
    return schemas.RunDetail(**base.model_dump(), attempts=[attempt_to_api(a) for a in attempts])


def event_to_api(event: models.Event) -> schemas.EventOut:
    return schemas.EventOut(
        id=event.id,
        attempt_id=event.attempt_id,
        turn_index=event.turn_index,
        seq=event.seq,
        type=EventType(event.type),
        payload=dict(event.payload or {}),
        latency_ms=event.latency_ms,
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        created_at=event.created_at,
    )


def turn_to_api(turn: models.Turn) -> schemas.TurnOut:
    return schemas.TurnOut(
        id=turn.id,
        attempt_id=turn.attempt_id,
        index=turn.index,
        user_message=turn.user_message,
        final_response=turn.final_response,
        passed=turn.passed,
        limit_exceeded=turn.limit_exceeded,
    )


def assertion_to_api(row: models.AssertionResult) -> schemas.AssertionResultOut:
    return schemas.AssertionResultOut(
        id=row.id,
        attempt_id=row.attempt_id,
        turn_index=row.turn_index,
        assertion_id=row.assertion_id,
        type=row.type,
        axis=Axis(row.axis),
        severity=Severity(row.severity),
        passed=row.passed,
        reason=row.reason,
        details=dict(row.details or {}),
        non_deterministic=row.non_deterministic,
        evaluable=row.evaluable,
    )


def scenario_summary(entry: ScenarioEntry) -> schemas.ScenarioSummary:
    loaded = entry.loaded
    if loaded is None:
        return schemas.ScenarioSummary(id=entry.id, title=entry.id, valid=False, error=entry.error)
    scenario = loaded.scenario
    return schemas.ScenarioSummary(
        id=scenario.id,
        title=scenario.title,
        description=scenario.description,
        version=scenario.version,
        axes=list(scenario.axes),
        turn_count=len(scenario.turns),
        tool_count=len(loaded.tools),
        assertion_count=sum(len(turn.assertions) for turn in scenario.turns),
        config_hash=loaded.config_hash,
        valid=True,
    )


def model_info(entry: ModelEntry) -> schemas.ModelInfo:
    """Registry entry for the UI. The API key value is never read or returned."""
    return schemas.ModelInfo(
        key=entry.key,
        display_name=entry.display_name,
        litellm_model=entry.litellm_model,
        provider=entry.litellm_model.split("/", 1)[0],
        params=dict(entry.params or {}),
        supports_parallel_tool_calls=entry.supports_parallel_tool_calls,
        supports_tool_calling=entry.supports_tool_calling,
        api_key_env=api_key_env_for(entry),
        api_key_present=api_key_present(entry),
        pricing_override=entry.pricing_override,
    )
