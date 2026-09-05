"""Structured logging.

Every line is JSON with the same shape, so a trip can be followed end to end by
filtering on one field:

    {"event": "tool_call", "trip_id": "abc", "agent": "transport_agent",
     "tool": "search_trains", "duration_ms": 0.8, "status": "ok"}

request_id and trip_id are bound once per request and appear on every line
logged underneath, including from the agents and the gateway, without any of
them having to pass the ids around.

Secrets are dropped by a processor rather than by remembering not to log them.
"""

import logging
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

from app.config import get_settings

_configured = False

# Field names whose values never reach the logs.
SECRET_FIELDS = re.compile(r"(api[_-]?key|password|secret|token|authorization)", re.IGNORECASE)

# Values that look like credentials even in an innocently named field.
SECRET_VALUES = re.compile(r"\b(sk-[A-Za-z0-9]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_\-]{20,})")

REDACTED = "[redacted]"


def redact_secrets(logger, method_name, event_dict):
    """Drop anything that looks like a credential, by field name or by shape."""
    for key, value in list(event_dict.items()):
        if SECRET_FIELDS.search(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str) and SECRET_VALUES.search(value):
            event_dict[key] = SECRET_VALUES.sub(REDACTED, value)
    return event_dict


def drop_empty(logger, method_name, event_dict):
    """Remove keys whose value is None.

    A caller that does not know the trip id passes None for it. Left in, that
    None would override the real trip id already bound to the context and the
    line could no longer be traced back to a trip.
    """
    return {key: value for key, value in event_dict.items() if value is not None}


def configure_logging() -> None:
    """Set up structlog. Called at import time by the API and the gateway.

    Without this, uvicorn's own configuration leaves our loggers below the root
    level and the tool-call lines never appear at all.
    """
    global _configured
    if _configured:
        return

    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Human-readable while developing, JSON everywhere else.
    renderer = (
        structlog.dev.ConsoleRenderer()
        if settings.app_env == "development"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            # Drop empties first, so a None never shadows a bound context value.
            drop_empty,
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Keep the standard library's loggers (uvicorn, sqlalchemy) readable too.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    _configured = True


def get_logger(name: str):
    return structlog.get_logger(name)


@contextmanager
def log_context(**fields) -> Iterator[None]:
    """Bind fields onto every log line written inside this block."""
    tokens = structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
