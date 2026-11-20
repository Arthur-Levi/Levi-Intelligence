from fastapi import APIRouter

from app.api.v1.endpoints.decision_makers import router as decision_makers_router
from app.api.v1.endpoints.identity import router as identity_router
from app.api.v1.endpoints.organizations import router as organizations_router
from app.api.v1.endpoints.prospecting import router as prospecting_router
from app.schemas.health import HealthResponse

router = APIRouter()
router.include_router(identity_router)
router.include_router(organizations_router)
router.include_router(prospecting_router)
router.include_router(decision_makers_router)


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def api_health() -> HealthResponse:
    return HealthResponse(status="ok", service="levi-intelligence-api")
