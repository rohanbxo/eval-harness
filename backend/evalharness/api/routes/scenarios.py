"""Scenario browsing (SPEC 9.2). Read-only: scenarios are authored in git."""

from __future__ import annotations

from fastapi import APIRouter

from evalharness.api.deps import get_scenario_or_404, get_scenarios
from evalharness.api.routes.serializers import scenario_summary
from evalharness.api.schemas import ScenarioDetail, ScenarioListResponse

router = APIRouter(tags=["scenarios"])


@router.get(
    "/scenarios",
    response_model=ScenarioListResponse,
    operation_id="listScenarios",
    summary="List scenarios with their validation status",
)
async def list_scenarios() -> ScenarioListResponse:
    return ScenarioListResponse(
        scenarios=[scenario_summary(entry) for entry in get_scenarios().values()]
    )


@router.get(
    "/scenarios/{scenario_id}",
    response_model=ScenarioDetail,
    operation_id="getScenario",
    summary="Full scenario definition, tools and fixtures",
)
async def get_scenario(scenario_id: str) -> ScenarioDetail:
    entry = get_scenario_or_404(scenario_id)
    loaded = entry.loaded
    if loaded is None:
        # The scenario exists on disk but does not load: say so explicitly rather
        # than 500-ing, so the UI can show the validation error.
        return ScenarioDetail(id=entry.id, config_hash="", valid=False, error=entry.error)

    transcripts: list[str] = []
    transcripts_dir = loaded.directory / "transcripts"
    if transcripts_dir.is_dir():
        transcripts = sorted(path.stem for path in transcripts_dir.glob("*.yaml"))

    return ScenarioDetail(
        id=loaded.id,
        config_hash=loaded.config_hash,
        valid=True,
        scenario=loaded.scenario,
        tools=list(loaded.tools),
        fixtures=dict(loaded.fixtures),
        expected=dict(loaded.expected),
        transcripts=transcripts,
    )
