"""Transport Agent preference rules and the mock transport provider."""

import pytest

from app.agents.transport_agent import research_transport
from app.tools import get_tools
from mcp_servers.transport.mock_data import search_buses, search_flights, search_trains

BRIEF = {
    "origin": "Kochi",
    "destination": "Delhi",
    "date": "2026-09-18",
    "travelers": 2,
    "preferred_mode": None,
    "max_journey_hours": None,
}


@pytest.fixture
def tools():
    return get_tools("transport_agent")


# --- provider ---------------------------------------------------------------


def test_results_are_stable_for_the_same_query():
    first = search_flights("Kochi", "Delhi", "2026-09-18", 2)
    assert first == search_flights("Kochi", "Delhi", "2026-09-18", 2)


def test_long_routes_have_no_buses():
    assert search_buses("Kochi", "Delhi", "2026-09-18", 2) == []


def test_short_routes_do_have_buses():
    assert search_buses("Kochi", "Bangalore", "2026-09-18", 2)


def test_cities_without_an_airport_have_no_flights():
    assert search_flights("Delhi", "Manali", "2026-09-18", 2) == []


def test_total_price_scales_with_passengers():
    for option in search_trains("Kochi", "Delhi", "2026-09-18", 3):
        assert option["total_price"] == option["price_per_person"] * 3


def test_unknown_city_is_rejected():
    with pytest.raises(ValueError, match="Unknown city"):
        search_flights("Atlantis", "Delhi", "2026-09-18", 2)


# --- agent preference rules -------------------------------------------------


def test_preferred_mode_within_the_time_limit_is_chosen(tools):
    """Kochi to Bangalore by train is under 12h, so the stated preference holds."""
    brief = {
        **BRIEF,
        "destination": "Bangalore",
        "preferred_mode": "train",
        "max_journey_hours": 12,
    }
    result = research_transport(brief, tools)
    assert result.recommended.mode == "train"
    assert "within the 12h limit" in result.reason


def test_preferred_mode_over_the_time_limit_falls_back(tools):
    """Kochi to Delhi by train is ~34h, so the 12h rule forces a flight."""
    brief = {**BRIEF, "preferred_mode": "train", "max_journey_hours": 12}
    result = research_transport(brief, tools)
    assert result.recommended.mode == "flight"
    assert "over the 12h limit" in result.reason


def test_without_a_preference_the_cheapest_option_wins(tools):
    result = research_transport(BRIEF, tools)
    cheapest = min([*result.alternatives, result.recommended], key=lambda o: o.price_per_person)
    assert result.recommended.id == cheapest.id


def test_a_failing_search_does_not_lose_the_other_modes(tools):
    def broken(**kwargs):
        raise RuntimeError("train MCP unavailable")

    result = research_transport(BRIEF, {**tools, "search_trains": broken})
    assert result.recommended is not None
    assert result.failures == ["train search unavailable"]


def test_no_options_at_all_is_reported_not_invented(tools):
    def empty(**kwargs):
        return []

    result = research_transport(BRIEF, dict.fromkeys(tools, empty))
    assert result.recommended is None
    assert "No transport options" in result.reason


def test_agent_only_returns_options_the_tools_returned(tools):
    result = research_transport(BRIEF, tools)
    real_ids = {
        o["id"]
        for search in (search_flights, search_trains, search_buses)
        for o in search("Kochi", "Delhi", "2026-09-18", 2)
    }
    assert result.recommended.id in real_ids
