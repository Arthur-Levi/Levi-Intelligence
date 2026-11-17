from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.db import get_engine
from app.schemas.health import HealthResponse

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # External clients and workers will be initialized here with explicit lifecycle management.
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Evidence-grounded B2B acquisition intelligence.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)
app.include_router(v1_router, prefix=settings.api_v1_prefix)


@app.get("/health/live", response_model=HealthResponse, tags=["system"])
async def liveness() -> HealthResponse:
    return HealthResponse(status="ok", service="levi-intelligence-api")


@app.get("/health/ready", response_model=HealthResponse, tags=["system"])
async def readiness() -> HealthResponse:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return HealthResponse(status="unavailable", service="levi-intelligence-api")
    return HealthResponse(status="ok", service="levi-intelligence-api")
