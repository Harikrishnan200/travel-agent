"""Logging configuration.

Both the API and the gateway call configure_logging() at import time, otherwise
uvicorn's own configuration leaves our loggers below the root level and the
tool-call lines never appear.

Phase 12 replaces the format string with structlog's JSON output; the call site
stays the same.
"""

import logging

from app.config import get_settings

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return

    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    _configured = True
