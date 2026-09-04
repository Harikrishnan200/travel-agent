"""Mock weather forecasts.

Deterministic per city and date, and seasonally plausible: the monsoon makes
rain likely in July/August, and hill stations are colder than the coast.
"""

import random
from datetime import date as date_type

from mcp_servers.cities import CITIES, UnknownCityError, normalize_city

# Cities that are notably cold, and their approximate temperature offset.
COLD_CITIES = {"Srinagar": -14, "Manali": -12, "Delhi": -2, "Jaipur": -1}

MONSOON_MONTHS = {6, 7, 8, 9}

CONDITIONS = [
    (70, "heavy rain"),
    (45, "light rain"),
    (25, "cloudy"),
    (0, "clear"),
]


def get_weather(city: str, date: str) -> dict:
    resolved = normalize_city(city)
    if resolved is None:
        raise UnknownCityError(city)
    day = date_type.fromisoformat(date)

    rng = random.Random(f"weather|{resolved}|{date}")

    base_rain = 55 if day.month in MONSOON_MONTHS else 15
    rain_chance = max(0, min(100, int(rng.gauss(base_rain, 22))))

    lat = CITIES[resolved][0]
    base_temp = 34 - (lat - 10) * 0.35 + COLD_CITIES.get(resolved, 0)
    if day.month in {12, 1, 2}:
        base_temp -= 8
    temperature = round(base_temp + rng.uniform(-3, 3), 1)

    condition = next(name for threshold, name in CONDITIONS if rain_chance >= threshold)

    return {
        "city": resolved,
        "date": date,
        "temperature_c": temperature,
        "condition": condition,
        "rain_probability": rain_chance,
        "good_for_outdoor": rain_chance < 45,
        "source": "mock",
    }
