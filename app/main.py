"""FastAPI application entrypoint."""

from fastapi import FastAPI

from app.api import trips
from app.config import get_settings

settings = get_settings()

app = FastAPI(
    title="AI Travel Planning Agent",
    description="Multi-agent travel planner built on LangGraph and MCP.",
    version="0.1.0",
)

app.include_router(trips.router)


@app.get("/health")
def health() -> dict:
    """Liveness probe. Reports config that changes behaviour, never secrets."""
    return {
        "status": "ok",
        "env": settings.app_env,
        "use_mock_data": settings.use_mock_data,
        "primary_llm_provider": settings.primary_llm_provider,
        "fallback_llm_provider": settings.fallback_llm_provider,
    }


@app.get("/")
def root() -> dict:
    return {"service": "ai-travel-planning-agent", "docs": "/docs", "health": "/health"}
