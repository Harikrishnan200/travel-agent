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


# --- the HTTP calls themselves ----------------------------------------------
# The parsing above is tested against captured payloads; these check the request
# we build and that a failure surfaces rather than being swallowed.


def tomorrow() -> str:
    """Open-Meteo only forecasts forward, so tests must ask about a future day."""
    from datetime import date, timedelta

    return (date.today() + timedelta(days=1)).isoformat()


def test_the_weather_request_asks_for_the_right_city_and_day(monkeypatch):
    captured = {}
    day = tomorrow()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"daily": {**OPEN_METEO_RESPONSE["daily"], "time": [day, "unused"]}}

    def fake_get(url, params=None, timeout=None):
        captured.update(url=url, params=params, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(weather_provider.httpx, "get", fake_get)

    forecast = weather_provider.get_weather("Delhi", day)

    assert captured["params"]["start_date"] == day
    assert captured["params"]["latitude"] == pytest.approx(28.7041)
    assert forecast["city"] == "Delhi"
    assert forecast["source"] == "open-meteo"


def test_a_weather_outage_is_raised_not_swallowed(monkeypatch):
    """The Experience Agent decides how to degrade; the provider must not decide for it."""

    def fake_get(*args, **kwargs):
        raise ConnectionError("open-meteo unreachable")

    monkeypatch.setattr(weather_provider.httpx, "get", fake_get)

    with pytest.raises(ConnectionError):
        weather_provider.get_weather("Delhi", tomorrow())


def test_a_date_in_the_past_is_refused():
    """Open-Meteo forecasts forward only; asking backwards would silently mislead."""
    with pytest.raises(ValueError, match="outside that window"):
        weather_provider.get_weather("Delhi", "2020-01-01")


def test_the_route_request_sends_longitude_before_latitude(monkeypatch):
    """OSRM takes lon,lat - the other way round from everything else here."""
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return OSRM_RESPONSE

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        return FakeResponse()

    monkeypatch.setattr(route_provider.httpx, "get", fake_get)

    route = route_provider.calculate_route("Delhi", "Agra")

    assert "77.1025,28.7041;78.0081,27.1767" in captured["url"]
    assert route["distance_km"] == 219.5


def test_an_unknown_city_never_reaches_the_network(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("should not have called out")

    monkeypatch.setattr(route_provider.httpx, "get", explode)

    from mcp_servers.cities import UnknownCityError

    with pytest.raises(UnknownCityError):
        route_provider.calculate_route("Atlantis", "Agra")
