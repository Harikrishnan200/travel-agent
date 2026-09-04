"""Real provider parsing, using captured API responses.

The parsing is tested offline so the suite never depends on a third-party API
being up. The live calls themselves are exercised by hand and in mock mode.
"""

import pytest

from mcp_servers.places import provider as route_provider
from mcp_servers.weather import provider as weather_provider

# Captured from api.open-meteo.com
OPEN_METEO_RESPONSE = {
    "daily": {
        "time": ["2026-09-04", "2026-09-05"],
        "temperature_2m_max": [32.0, 28.2],
        "temperature_2m_min": [26.4, 25.0],
        "precipitation_probability_max": [94, 20],
        "weather_code": [95, 1],
    }
}

# Captured from router.project-osrm.org
OSRM_RESPONSE = {
    "code": "Ok",
    "routes": [{"distance": 219504.1, "duration": 10215.1}],
}


def test_a_wet_day_is_parsed_as_unsuitable_for_outdoor_plans():
    forecast = weather_provider.parse_forecast("Delhi", "2026-09-04", OPEN_METEO_RESPONSE)
    assert forecast["condition"] == "heavy rain"
    assert forecast["rain_probability"] == 94
    assert forecast["good_for_outdoor"] is False
    assert forecast["source"] == "open-meteo"


def test_a_dry_day_is_parsed_as_suitable():
    forecast = weather_provider.parse_forecast("Delhi", "2026-09-05", OPEN_METEO_RESPONSE)
    assert forecast["condition"] == "clear"
    assert forecast["good_for_outdoor"] is True


def test_temperature_is_the_midpoint_of_the_daily_range():
    forecast = weather_provider.parse_forecast("Delhi", "2026-09-04", OPEN_METEO_RESPONSE)
    assert forecast["temperature_c"] == pytest.approx((32.0 + 26.4) / 2)


def test_the_real_provider_returns_the_same_shape_as_the_mock():
    from mcp_servers.weather.mock_data import get_weather as mock_weather

    real = weather_provider.parse_forecast("Delhi", "2026-09-04", OPEN_METEO_RESPONSE)
    assert set(real) == set(mock_weather("Delhi", "2026-09-04"))


def test_dates_beyond_the_forecast_window_are_refused_not_guessed():
    with pytest.raises(ValueError, match="outside that window"):
        weather_provider.get_weather("Delhi", "2099-01-01")


def test_osrm_distance_is_converted_to_kilometres_and_hours():
    route = route_provider.parse_route("Delhi", "Agra", OSRM_RESPONSE)
    assert route["distance_km"] == 219.5
    assert route["duration_hours"] == 2.8
    assert route["source"] == "osrm"


def test_a_route_osrm_cannot_find_is_an_error():
    with pytest.raises(ValueError, match="could not route"):
        route_provider.parse_route("Delhi", "Agra", {"code": "NoRoute", "routes": []})


def test_the_real_route_returns_the_same_shape_as_the_mock():
    from mcp_servers.places.mock_data import calculate_route as mock_route

    real = route_provider.parse_route("Delhi", "Agra", OSRM_RESPONSE)
    assert set(real) == set(mock_route("Delhi", "Agra"))


def test_unknown_weather_codes_do_not_crash():
    assert weather_provider.describe_code(999) == "unknown"
