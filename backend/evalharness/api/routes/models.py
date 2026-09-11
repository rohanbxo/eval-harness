"""Model registry endpoint (SPEC 9.2).

Reports *whether* each model's API key is configured. It never reads, returns or
logs the key itself -- the run launcher only needs to know which models to grey
out.
"""

from __future__ import annotations

from fastapi import APIRouter

from evalharness.api.deps import get_registry
from evalharness.api.routes.serializers import model_info
from evalharness.api.schemas import ModelListResponse

router = APIRouter(tags=["models"])


@router.get(
    "/models",
    response_model=ModelListResponse,
    operation_id="listModels",
    summary="Model registry, flagged with API key availability",
)
async def list_models() -> ModelListResponse:
    registry = get_registry()
    return ModelListResponse(models=[model_info(entry) for entry in registry.models])
