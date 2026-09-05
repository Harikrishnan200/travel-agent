"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import trips
from app.config import get_settings
from app.logging_setup import configure_logging

configure_logging()
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the tables on start, close the connection pool on stop."""
    from app.db.checkpointer import close_checkpointer
    from app.db.database import create_tables

    create_tables()
    yield
    close_checkpointer()


app = FastAPI(
    title="AI Travel Planning Agent",
    description="Multi-agent travel planner built on LangGraph and MCP.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(trips.router)


@app.get("/health")
def health() -> dict:
    """Liveness probe. Reports config that changes behaviour, never secrets."""
    from app.db.database import get_engine

    return {
        "status": "ok",
        "env": settings.app_env,
        "use_mock_data": settings.use_mock_data,
        "primary_llm_provider": settings.primary_llm_provider,
        "fallback_llm_provider": settings.fallback_llm_provider,
        "database": "connected" if get_engine() is not None else "unavailable",
    }


@app.get("/")
def root() -> dict:
    return {"service": "ai-travel-planning-agent", "docs": "/docs", "health": "/health"}
