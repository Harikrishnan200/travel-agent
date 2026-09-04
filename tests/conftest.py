"""Shared test setup."""

import pytest

from gateway import service


@pytest.fixture(autouse=True)
def fresh_rate_limits():
    """Each test gets a clean rate-limit window.

    The counters are process-wide, so without this one test's tool calls would
    count against the next one's budget.
    """
    service.reset_rate_limits()
    yield
    service.reset_rate_limits()
