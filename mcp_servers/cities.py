"""Shared city reference data.

One coordinate table drives distances, journey times and prices across all the
mock providers, so a Kochi->Delhi trip is expensive and long everywhere without
hand-writing 45 city pairs.
"""

import math

# name -> (latitude, longitude, has_airport, has_railway)
CITIES: dict[str, tuple[float, float, bool, bool]] = {
    "Kochi": (9.9312, 76.2673, True, True),
    "Bangalore": (12.9716, 77.5946, True, True),
    "Chennai": (13.0827, 80.2707, True, True),
    "Delhi": (28.7041, 77.1025, True, True),
    "Mumbai": (19.0760, 72.8777, True, True),
    "Hyderabad": (17.3850, 78.4867, True, True),
    "Jaipur": (26.9124, 75.7873, True, True),
    "Srinagar": (34.0837, 74.7973, True, False),
    "Goa": (15.2993, 74.1240, True, True),
    "Manali": (32.2432, 77.1892, False, False),
    "Agra": (27.1767, 78.0081, False, True),
}


def normalize_city(name: str) -> str | None:
    """Match a user-supplied city name case-insensitively. None if unknown."""
    if not name:
        return None
    lookup = {city.lower(): city for city in CITIES}
    return lookup.get(name.strip().lower())


def distance_km(origin: str, destination: str) -> float:
    """Great-circle distance between two known cities."""
    lat1, lon1, _, _ = CITIES[origin]
    lat2, lon2, _, _ = CITIES[destination]
    radius = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return round(2 * radius * math.asin(math.sqrt(a)), 1)


def has_airport(city: str) -> bool:
    return CITIES[city][2]


def has_railway(city: str) -> bool:
    return CITIES[city][3]


class UnknownCityError(ValueError):
    """Raised when a city is not in the reference table."""

    def __init__(self, city: str):
        super().__init__(f"Unknown city: {city!r}. Known cities: {', '.join(sorted(CITIES))}")
