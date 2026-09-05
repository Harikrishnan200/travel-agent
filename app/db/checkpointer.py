"""The LangGraph checkpointer, which is what makes a trip resumable.

The workflow state is written to Postgres after every node. That is what lets
the API pause at human review, return, and pick the same trip up again later -
including after a restart. Without a database we fall back to an in-memory
saver: everything still works, but a restart forgets.

The connection pool is opened once and kept for the life of the process, which
is why this is not a context manager.
"""

import logging

from langgraph.checkpoint.memory import MemorySaver

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_checkpointer = None


def get_checkpointer():
    """A Postgres checkpointer, or an in-memory one if Postgres is unreachable."""
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        # LangGraph writes rows itself, so autocommit is required.
        pool = ConnectionPool(
            conninfo=_psycopg_url(settings.database_url),
            max_size=settings.db_pool_size,
            kwargs={"autocommit": True, "row_factory": _dict_row()},
            open=True,
        )
        checkpointer = PostgresSaver(pool)
        checkpointer.setup()
        _checkpointer = checkpointer
        logger.info("using postgres checkpointer, trips survive a restart")
    except Exception as exc:
        logger.warning("postgres checkpointer unavailable, trips are in memory only: %s", exc)
        _checkpointer = MemorySaver()

    return _checkpointer


def set_checkpointer(checkpointer) -> None:
    """Point the app at a specific checkpointer. Used by tests."""
    global _checkpointer
    _checkpointer = checkpointer


def _psycopg_url(url: str) -> str:
    """SQLAlchemy uses postgresql+psycopg://; psycopg itself wants postgresql://."""
    return url.replace("postgresql+psycopg://", "postgresql://")


def _dict_row():
    from psycopg.rows import dict_row

    return dict_row


def close_checkpointer() -> None:
    """Close the connection pool on shutdown, so the process exits cleanly."""
    global _checkpointer
    pool = getattr(_checkpointer, "conn", None)
    if pool is not None and hasattr(pool, "close"):
        try:
            pool.close()
        except Exception as exc:
            logger.warning("closing the checkpointer pool failed: %s", exc)
    _checkpointer = None
