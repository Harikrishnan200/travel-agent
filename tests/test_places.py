"""Experience Agent, places and weather providers."""

import pytest

from app.agents.experience_agent import research_experience
from app.tools import get_tools
from mcp_servers.places.mock_data import calculate_route, search_places
from mcp_servers.weather.mock_data import get_weather

BRIEF = {
    "origin": "Kochi",
    "city": "Delhi",
    "start_date": "2026-09-18",
    "duration_days": 5,
    "interests": ["historical", "food"],
    "travelers": 2,
}


@pytest.fixture
def tools():
    return get_tools("experience_agent")


# --- providers --------------------------------------------------------------


def test_places_are_filtered_by_category():
    assert all(p["category"] == "historical" for p in search_places("Delhi", "historical"))


def test_unknown_category_is_rejected():
    with pytest.raises(ValueError, match="Unknown category"):
        search_places("Delhi", "teleportation")


def test_max_results_is_respected():
    assert len(search_places("Delhi", max_results=3)) == 3


def test_route_distance_exceeds_straight_line():
    route = calculate_route("Delhi", "Agra")
    assert route["distance_km"] > 190  # straight line is ~192km
    assert route["duration_hours"] > 0


def test_weather_is_stable_and_seasonal():
    assert get_weather("Delhi", "2026-09-18") == get_weather("Delhi", "2026-09-18")
    monsoon = [get_weather("Goa", f"2026-07-{d:02d}")["rain_probability"] for d in range(1, 20)]
    winter = [get_weather("Goa", f"2026-01-{d:02d}")["rain_probability"] for d in range(1, 20)]
    assert sum(monsoon) / len(monsoon) > sum(winter) / len(winter)


def test_hill_stations_are_colder_than_the_coast():
    assert (
        get_weather("Srinagar", "2026-01-10")["temperature_c"]
        < get_weather("Kochi", "2026-01-10")["temperature_c"]
    )


# --- agent ------------------------------------------------------------------


def test_a_forecast_is_fetched_for_every_trip_day(tools):
    result = research_experience(BRIEF, tools)
    assert len(result.weather) == BRIEF["duration_days"]


def test_only_places_matching_the_interests_are_returned(tools):
    result = research_experience(BRIEF, tools)
    assert {p.category for p in result.places} <= {"historical", "food"}


def test_every_day_receives_at_least_one_activity(tools):
    result = research_experience(BRIEF, tools)
    assert len(result.suggested_schedule) == BRIEF["duration_days"]
    assert all(day["activities"] for day in result.suggested_schedule)


def test_arrival_and_departure_days_are_lighter(tools):
    schedule = research_experience(BRIEF, tools).suggested_schedule
    assert len(schedule[0]["activities"]) == 1
    assert len(schedule[-1]["activities"]) == 1


def test_outdoor_activities_land_on_the_driest_days(tools):
    """A dry-season trip has room to choose, so the outdoor plans go on the best days."""
    result = research_experience({**BRIEF, "start_date": "2026-12-10"}, tools)
    outdoor_rain = []
    indoor_rain = []
    for day in result.suggested_schedule:
        rain = day["weather"]["rain_probability"]
        for activity in day["activities"]:
            (outdoor_rain if activity["outdoor"] else indoor_rain).append(rain)
    if outdoor_rain and indoor_rain:
        assert min(outdoor_rain) <= min(indoor_rain)


def test_a_wet_day_with_outdoor_plans_is_flagged(tools):
    result = research_experience(BRIEF, tools)  # September in Delhi is monsoon
    assert any("rain" in note for note in result.notes)


def test_a_failing_weather_lookup_does_not_stop_the_agent(tools):
    def broken(**kwargs):
        raise RuntimeError("weather MCP down")

    result = research_experience(BRIEF, {**tools, "get_weather": broken})
    assert result.places
    assert result.weather == []
    assert len(result.failures) == BRIEF["duration_days"]
