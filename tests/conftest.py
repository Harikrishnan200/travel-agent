"""Shared test setup.

Tests run against in-memory infrastructure by default: a fake Redis, an
in-memory LangGraph checkpointer and no database. The suite therefore never
depends on a running Postgres or Redis, and never writes to a real one.

tests/test_persistence.py opts back in to a real Postgres, and skips itself when
there is not one.
"""

import fakeredis
import pytest
from langgraph.checkpoint.memory import MemorySaver

from app import cache
from app.api import trips
from app.config import get_settings
from app.db import database
from gateway import service


@pytest.fixture(autouse=True)
def no_outbound_api_calls(monkeypatch):
    """No test may call a real external API.

    With keys in .env the suite would otherwise make live calls: slow, metered,
    and dependent on someone else's uptime. Every optional provider is switched
    off by clearing its key, which is also the configuration most people run.
    Tests that need a provider fake it, or set their own key.
    """
    settings = get_settings()
    for key in (
        "groq_api_key",
        "gemini_api_key",
        "aviation_stack_api_key",
        "tavily_api_key",
    ):
        monkeypatch.setattr(settings, key, "")


@pytest.fixture(autouse=True)
def isolated_infrastructure():
    """A fresh cache, a fresh checkpointer and no database, for each test.

    Without the cache reset, one test's cached tool results are served to the
    next - which once hid a failure-path assertion.
    """
    client = fakeredis.FakeRedis(decode_responses=True)
    cache.set_client(client)
    service.reset_rate_limits()

    database.set_engine(None)
    trips.reset_graph()
    from app.db import checkpointer

    checkpointer.set_checkpointer(MemorySaver())

    yield client

    client.flushall()
    cache.set_client(None)
    trips.reset_graph()
    checkpointer.set_checkpointer(None)


@pytest.fixture
def no_redis():
    """Simulate Redis being down, so the fallback paths can be tested."""
    cache.set_client(None)
    service.reset_rate_limits()
    yield
    service.reset_rate_limits()
