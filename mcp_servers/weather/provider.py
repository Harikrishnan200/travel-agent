"""Real weather, from Open-Meteo.

Free, no API key, no rate limit worth worrying about at this scale. It only
forecasts about two weeks ahead, so a date beyond that raises rather than
quietly returning something invented - the Experience Agent degrades honestly
instead of planning around a guess.
"""

from datetime import date as date_type

import httpx

from mcp_servers.cities import UnknownCityError, normalize_city
from mcp_servers.places.mock_data import city_coordinates


class OutsideForecastWindow(ValueError):
    """The date is further ahead than anyone can forecast."""


API_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS = "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
FORECAST_DAYS = 16

# WMO weather codes, grouped into the handful of conditions we schedule around.
# https://open-meteo.com/en/docs
WMO_CONDITIONS = [
    (range(0, 2), "clear"),
    (range(2, 4), "cloudy"),
    (range(45, 49), "fog"),
    (range(51, 58), "light rain"),
    (range(61, 66), "light rain"),
    (range(66, 68), "heavy rain"),
    (range(71, 78), "snow"),
    (range(80, 83), "light rain"),
    (range(85, 87), "snow"),
    (range(95, 100), "heavy rain"),
]

# Above this chance of rain we treat a day as unsuitable for outdoor plans.
OUTDOOR_RAIN_THRESHOLD = 45


def get_weather(city: str, date: str, timeout: float = 10.0) -> dict:
    resolved = normalize_city(city)
    if resolved is None:
        raise UnknownCityError(city)

    days_ahead = (date_type.fromisoformat(date) - date_type.today()).days
    if not 0 <= days_ahead < FORECAST_DAYS:
        raise OutsideForecastWindow(
            f"Open-Meteo forecasts {FORECAST_DAYS} days ahead; {date} is outside that window."
        )

    latitude, longitude = city_coordinates(resolved)
    response = httpx.get(
        API_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "daily": DAILY_FIELDS,
            "start_date": date,
            "end_date": date,
            "timezone": "auto",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_forecast(resolved, date, response.json())


def parse_forecast(city: str, date: str, payload: dict) -> dict:
    """Turn an Open-Meteo response into the same shape the mock provider returns."""
    daily = payload["daily"]
    index = daily["time"].index(date)

    high = daily["temperature_2m_max"][index]
    low = daily["temperature_2m_min"][index]
    rain = daily["precipitation_probability_max"][index] or 0
    code = daily["weather_code"][index]

    return {
        "city": city,
        "date": date,
        "temperature_c": round((high + low) / 2, 1),
        "condition": describe_code(code),
        "rain_probability": int(rain),
        "good_for_outdoor": rain < OUTDOOR_RAIN_THRESHOLD,
        "source": "open-meteo",
    }


def describe_code(code: int) -> str:
    for codes, condition in WMO_CONDITIONS:
        if code in codes:
            return condition
    return "unknown"
