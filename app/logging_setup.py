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

# Secrets in URLs. Libraries log the URLs they fetch, and a key in a query
# string ends up in the logs however careful our own logging is - httpx logs
# "GET https://api...?access_key=..." at INFO, which is where this was found.
SECRET_QUERY_PARAMS = re.compile(
    r"((?:access_key|api_key|apikey|key|token|password)=)[^&\s\"']+", re.IGNORECASE
)


class RedactingFilter(logging.Filter):
    """Strip credentials out of standard-library log records.

    structlog handles our own lines; this covers everything written by the
    libraries we use, which is where a key is most likely to escape.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)

        if isinstance(record.args, dict):
            record.args = {key: _redact_arg(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_redact_arg(arg) for arg in record.args)
        return True


def _redact_arg(arg):
    """Redact one log argument, leaving anything without a secret exactly as it was.

    Checking only for str would miss the important case: httpx logs the URL it
    fetched as an httpx.URL object, not a string, which is how an API key in a
    query string reached the logs in the first place. Numbers are returned
    untouched so the %d and %f placeholders that uvicorn uses still work.
    """
    if arg is None or isinstance(arg, bool | int | float):
        return arg

    text = str(arg)
    cleaned = redact_text(text)
    # Only replace when something was actually removed, so an object keeps its
    # own formatting in the overwhelmingly common case.
    return cleaned if cleaned != text else arg


def redact_text(text: str) -> str:
    """Remove credentials from a line of text, by query parameter or by shape."""
    text = SECRET_QUERY_PARAMS.sub(rf"\1{REDACTED}", text)
    return SECRET_VALUES.sub(REDACTED, text)


def redact_secrets(logger, method_name, event_dict):
    """Drop anything that looks like a credential, by field name or by shape."""
    for key, value in list(event_dict.items()):
        if SECRET_FIELDS.search(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = redact_text(value)
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

    # Keep the standard library's loggers (uvicorn, httpx, sqlalchemy) readable,
    # and run every one of their records through the redaction filter.
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter())

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
