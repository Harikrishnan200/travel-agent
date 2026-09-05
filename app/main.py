"""FastAPI application entrypoint."""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from app import metrics
from app.api import trips
from app.config import get_settings
from app.logging_setup import configure_logging, get_logger, log_context, new_request_id

configure_logging()
logger = get_logger(__name__)
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


@app.middleware("http")
async def add_request_context(request: Request, call_next):
    """Give every request an id, and put it on every log line it produces.

    The id also comes back as a header, so a user reporting a problem can quote
    it and the whole request can be found in the logs.
    """
    request_id = request.headers.get("X-Request-ID") or new_request_id()
    started = time.monotonic()

    with log_context(request_id=request_id, path=request.url.path):
        response = await call_next(request)

    duration_ms = round((time.monotonic() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    # On every endpoint, including the ones whose body has no room for it.
    response.headers["X-Response-Time-Ms"] = str(duration_ms)
    logger.info("request_finished", status_code=response.status_code, duration_ms=duration_ms)
    return response


@app.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint."""
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)


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
