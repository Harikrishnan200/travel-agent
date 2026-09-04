"""Mock attractions and point-to-point routes."""

import random

from mcp_servers.cities import CITIES, UnknownCityError, distance_km, normalize_city

CATEGORIES = ["historical", "nature", "food", "culture", "adventure", "shopping"]

# A few real landmarks per city keep demos believable; the generator fills the rest.
KNOWN_PLACES: dict[str, list[tuple[str, str]]] = {
    "Delhi": [
        ("Red Fort", "historical"),
        ("Humayun's Tomb", "historical"),
        ("Qutub Minar", "historical"),
        ("India Gate", "historical"),
        ("Chandni Chowk Food Walk", "food"),
        ("Lodhi Gardens", "nature"),
        ("National Museum", "culture"),
        ("Dilli Haat", "shopping"),
    ],
    "Kochi": [
        ("Fort Kochi Beach", "nature"),
        ("Mattancherry Palace", "historical"),
        ("Chinese Fishing Nets", "culture"),
        ("Jew Town Spice Market", "shopping"),
        ("Kathakali Performance", "culture"),
        ("Kerala Backwater Cruise", "nature"),
    ],
    "Jaipur": [
        ("Amber Fort", "historical"),
        ("Hawa Mahal", "historical"),
        ("City Palace", "historical"),
        ("Johari Bazaar", "shopping"),
        ("Nahargarh Fort", "historical"),
    ],
    "Goa": [
        ("Calangute Beach", "nature"),
        ("Basilica of Bom Jesus", "historical"),
        ("Dudhsagar Falls", "adventure"),
        ("Anjuna Flea Market", "shopping"),
    ],
    "Agra": [
        ("Taj Mahal", "historical"),
        ("Agra Fort", "historical"),
        ("Mehtab Bagh", "nature"),
    ],
    "Srinagar": [
        ("Dal Lake Shikara Ride", "nature"),
        ("Mughal Gardens", "historical"),
        ("Gulmarg Gondola", "adventure"),
    ],
}

GENERIC = [
    ("{city} Museum", "culture"),
    ("{city} Old Fort", "historical"),
    ("{city} Botanical Garden", "nature"),
    ("{city} Street Food Lane", "food"),
    ("{city} Central Market", "shopping"),
    ("{city} Adventure Park", "adventure"),
]

# Roughly how long a visit takes and whether rain ruins it.
CATEGORY_PROFILE = {
    "historical": (3.0, True),
    "nature": (2.5, True),
    "adventure": (4.0, True),
    "food": (2.0, False),
    "culture": (2.0, False),
    "shopping": (2.0, False),
}


def search_places(city: str, category: str | None = None, max_results: int = 10) -> list[dict]:
    resolved = normalize_city(city)
    if resolved is None:
        raise UnknownCityError(city)
    if category is not None and category not in CATEGORIES:
        raise ValueError(f"Unknown category {category!r}. Valid: {', '.join(CATEGORIES)}")

    rng = random.Random(f"places|{resolved}")
    entries = list(KNOWN_PLACES.get(resolved, []))
    entries += [(template.format(city=resolved), cat) for template, cat in GENERIC]

    places = []
    for name, cat in entries:
        duration, outdoor = CATEGORY_PROFILE[cat]
        places.append(
            {
                "name": name,
                "city": resolved,
                "category": cat,
                "rating": round(rng.uniform(3.5, 4.9), 1),
                "entry_fee": rng.choice([0, 0, 50, 200, 500, 600]),
                "visit_duration_hours": duration,
                "outdoor": outdoor,
            }
        )

    if category:
        places = [p for p in places if p["category"] == category]
    places.sort(key=lambda p: -p["rating"])
    return places[:max_results]


def calculate_route(origin: str, destination: str) -> dict:
    """Straight-line distance with a road factor. Not a routing engine."""
    start, end = normalize_city(origin), normalize_city(destination)
    if start is None:
        raise UnknownCityError(origin)
    if end is None:
        raise UnknownCityError(destination)

    straight = distance_km(start, end)
    road_km = round(straight * 1.25, 1)  # roads are not straight lines
    return {
        "origin": start,
        "destination": end,
        "distance_km": road_km,
        "duration_hours": round(road_km / 50, 1),
        "mode": "driving",
        "source": "mock",
    }


def city_coordinates(city: str) -> tuple[float, float]:
    resolved = normalize_city(city)
    if resolved is None:
        raise UnknownCityError(city)
    lat, lon, _, _ = CITIES[resolved]
    return lat, lon
