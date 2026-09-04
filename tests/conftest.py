"""Shared test setup.

Every test gets its own empty cache and a clean rate-limit window. Without that,
one test's cached tool results would be served to the next one - which silently
hid a failure-path test the first time this suite ran against a live Redis.
"""

import fakeredis
import pytest

from app import cache
from gateway import service


@pytest.fixture(autouse=True)
def isolated_cache():
    """Point the cache helpers at a fresh in-memory Redis for each test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    cache.set_client(client)
    service.reset_rate_limits()
    yield client
    client.flushall()
    cache.set_client(None)


@pytest.fixture
def no_redis():
    """Simulate Redis being down, so the fallback paths can be tested."""
    cache.set_client(None)
    service.reset_rate_limits()
    yield
    service.reset_rate_limits()
