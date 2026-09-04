"""Mock hotel inventory, deterministic per city and date."""

import random
from datetime import date as date_type

from mcp_servers.cities import UnknownCityError, normalize_city

BRANDS = ["Grand", "Sunrise", "Heritage", "Comfort", "Royal", "Riverside", "Central", "Palm"]
KINDS = ["Hotel", "Residency", "Inn", "Suites", "Guest House"]
AREAS = {
    "Delhi": ["Connaught Place", "Karol Bagh", "Aerocity", "Paharganj"],
    "Kochi": ["Fort Kochi", "Marine Drive", "Kakkanad"],
    "Jaipur": ["Bani Park", "MI Road", "Amer Road"],
    "Goa": ["Calangute", "Panjim", "Anjuna"],
    "Mumbai": ["Colaba", "Andheri", "Bandra"],
}
DEFAULT_AREAS = ["City Centre", "Station Road", "Old Town"]

AMENITIES = ["wifi", "breakfast", "ac", "parking", "pool", "gym", "restaurant", "airport shuttle"]


def nights_between(check_in: str, check_out: str) -> int:
    """Nights between two ISO dates. Raises ValueError on a bad range."""
    start, end = date_type.fromisoformat(check_in), date_type.fromisoformat(check_out)
    nights = (end - start).days
    if nights < 1:
        raise ValueError(f"check_out ({check_out}) must be after check_in ({check_in})")
    return nights


def search_hotels(
    city: str, check_in: str, check_out: str, guests: int, budget: float | None = None
) -> list[dict]:
    """Hotels in a city. `budget` is the total accommodation budget for the stay."""
    resolved = normalize_city(city)
    if resolved is None:
        raise UnknownCityError(city)
    nights = nights_between(check_in, check_out)

    rng = random.Random(f"hotel|{resolved}|{check_in}|{check_out}")
    areas = AREAS.get(resolved, DEFAULT_AREAS)

    hotels = []
    for _ in range(8):
        rating = round(rng.uniform(3.0, 4.8), 1)
        # Price tracks rating: a 4.8 star room is not the price of a 3.0 star one.
        price = int((700 + (rating - 3.0) * 2200) * rng.uniform(0.8, 1.25) / 50) * 50
        rooms = (guests + 1) // 2  # two guests per room
        hotels.append(
            {
                "name": f"{rng.choice(BRANDS)} {rng.choice(KINDS)}",
                "city": resolved,
                "location": rng.choice(areas),
                "rating": rating,
                "price_per_night": price,
                "rooms_required": rooms,
                "nights": nights,
                "total_price": price * rooms * nights,
                "amenities": sorted(rng.sample(AMENITIES, k=rng.randint(3, 6))),
            }
        )

    if budget is not None:
        hotels = [h for h in hotels if h["total_price"] <= budget]
    return sorted(hotels, key=lambda h: (-h["rating"], h["price_per_night"]))
