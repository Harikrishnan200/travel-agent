"""Accommodation Agent and the mock hotel provider."""

import pytest

from app.agents.accommodation_agent import research_accommodation
from app.tools import get_tools
from mcp_servers.hotels.mock_data import nights_between, search_hotels

BRIEF = {
    "city": "Delhi",
    "check_in": "2026-09-18",
    "check_out": "2026-09-22",
    "travelers": 2,
    "max_total_price": 20000.0,
}


@pytest.fixture
def tools():
    return get_tools("accommodation_agent")


# --- provider ---------------------------------------------------------------


def test_nights_are_counted_between_the_dates():
    assert nights_between("2026-09-18", "2026-09-22") == 4


def test_check_out_before_check_in_is_rejected():
    with pytest.raises(ValueError, match="must be after"):
        nights_between("2026-09-22", "2026-09-18")


def test_total_price_covers_rooms_and_nights():
    for hotel in search_hotels("Delhi", "2026-09-18", "2026-09-22", 4):
        assert hotel["rooms_required"] == 2  # two guests per room
        assert hotel["total_price"] == hotel["price_per_night"] * 2 * 4


def test_budget_filter_excludes_expensive_hotels():
    within = search_hotels("Delhi", "2026-09-18", "2026-09-22", 2, budget=8000)
    assert all(h["total_price"] <= 8000 for h in within)


# --- agent ------------------------------------------------------------------


def test_recommended_hotel_fits_the_allocated_budget(tools):
    result = research_accommodation(BRIEF, tools)
    assert result.recommended.total_price <= BRIEF["max_total_price"]
    assert "within the" in result.reason


def test_best_rating_that_fits_is_preferred(tools):
    result = research_accommodation(BRIEF, tools)
    affordable = [
        h
        for h in [result.recommended, *result.alternatives]
        if h.total_price <= BRIEF["max_total_price"]
    ]
    assert result.recommended.rating == max(h.rating for h in affordable)


def test_an_unaffordable_city_still_returns_the_cheapest_and_says_so(tools):
    result = research_accommodation({**BRIEF, "max_total_price": 100.0}, tools)
    assert result.recommended is not None
    assert "No hotel fits" in result.reason
    # The validator, not the agent, is what fails the plan.
    assert result.recommended.total_price > 100


def test_a_failing_search_is_reported_not_faked(tools):
    def broken(**kwargs):
        raise RuntimeError("hotel MCP down")

    result = research_accommodation(BRIEF, {"search_hotels": broken})
    assert result.recommended is None
    assert result.failures == ["hotel search unavailable"]
