"""Redis caching and rate limiting.

Redis is a speed-up and a safety rail, never a source of truth. Every helper
here swallows connection errors and reports failure, so the system runs (more
slowly) with Redis down rather than falling over.
"""

import hashlib
import json
import logging
from typing import Any

import redis

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# How long each kind of result stays useful. Weather changes hourly, hotel and
# transport prices drift within a day, a city's landmarks do not move.
TOOL_TTL_SECONDS = {
    "get_weather": 3600,
    "search_flights": 900,
    "search_trains": 900,
    "search_buses": 900,
    "search_hotels": 900,
    "search_places": 86400,
    "calculate_route": 86400,
}
DEFAULT_TTL_SECONDS = 900

_client: redis.Redis | None = None
_unavailable = False


def get_client() -> redis.Redis | None:
    """A shared Redis client, or None if Redis cannot be reached."""
    global _client, _unavailable
    if _unavailable:
        return None
    if _client is None:
        try:
            _client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
            _client.ping()
        except Exception as exc:
            logger.warning("redis unavailable, running without cache: %s", exc)
            _client, _unavailable = None, True
    return _client


def set_client(client: redis.Redis | None) -> None:
    """Point the helpers at a specific client. Used by tests to inject fakeredis."""
    global _client, _unavailable
    _client, _unavailable = client, client is None


def make_key(tool: str, arguments: dict) -> str:
    """A stable key for one tool call. Sorted, so argument order cannot change it."""
    payload = json.dumps(arguments, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"tool:{tool}:{digest}"


def get(key: str) -> Any | None:
    client = get_client()
    if client is None:
        return None
    try:
        cached = client.get(key)
    except Exception as exc:
        logger.warning("redis read failed: %s", exc)
        return None
    return json.loads(cached) if cached else None


def set(key: str, value: Any, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
    client = get_client()
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps(value, default=str))
    except Exception as exc:
        logger.warning("redis write failed: %s", exc)


def ttl_for(tool: str) -> int:
    return TOOL_TTL_SECONDS.get(tool, DEFAULT_TTL_SECONDS)


def increment_window(key: str, window_seconds: int) -> int | None:
    """Count one call in a fixed window. Returns the new count, or None if Redis is down.

    A None tells the caller to fall back to its own counter rather than to let
    every call through unchecked.
    """
    client = get_client()
    if client is None:
        return None
    try:
        pipeline = client.pipeline()
        pipeline.incr(key)
        pipeline.expire(key, window_seconds)
        count, _ = pipeline.execute()
    except Exception as exc:
        logger.warning("redis rate limit check failed: %s", exc)
        return None
    return int(count)


def clear() -> None:
    """Drop everything. Tests only."""
    client = get_client()
    if client is not None:
        try:
            client.flushdb()
        except Exception:
            pass
