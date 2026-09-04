"""Mock flight, train and bus inventory.

Deterministic: the same route and date always produce the same options, so
tests, caching and re-planning all see stable data. Durations and prices are
derived from real distance, not hand-written per city pair.
"""

import random

from mcp_servers.cities import (
    UnknownCityError,
    distance_km,
    has_airport,
    has_railway,
    normalize_city,
)

AIRLINES = [("6E", "IndiGo"), ("AI", "Air India"), ("SG", "SpiceJet"), ("UK", "Vistara")]
TRAIN_NAMES = ["Kerala Express", "Rajdhani Express", "Duronto Express", "Superfast Express"]
BUS_OPERATORS = ["KSRTC", "VRL Travels", "SRS Travels", "Orange Tours"]

# Buses stop being a realistic option on very long routes.
MAX_BUS_DISTANCE_KM = 1200


def _rng(*parts: object) -> random.Random:
    """A generator seeded by the query, so results are stable but varied."""
    return random.Random("|".join(str(p) for p in parts))


def _resolve(origin: str, destination: str) -> tuple[str, str]:
    start, end = normalize_city(origin), normalize_city(destination)
    if start is None:
        raise UnknownCityError(origin)
    if end is None:
        raise UnknownCityError(destination)
    return start, end


def search_flights(origin: str, destination: str, date: str, passengers: int) -> list[dict]:
    start, end = _resolve(origin, destination)
    if not (has_airport(start) and has_airport(end)):
        return []

    km = distance_km(start, end)
    rng = _rng("flight", start, end, date)
    hours = round(km / 700 + 0.75, 1)

    options = []
    for code, airline in rng.sample(AIRLINES, k=3):
        price = int((2200 + km * 2.1) * rng.uniform(0.85, 1.3) / 50) * 50
        depart_hour = rng.choice([6, 9, 13, 17, 20])
        options.append(
            {
                "id": f"{code}-{rng.randint(100, 999)}",
                "mode": "flight",
                "provider": airline,
                "origin": start,
                "destination": end,
                "date": date,
                "depart_time": f"{depart_hour:02d}:00",
                "duration_hours": hours,
                "price_per_person": price,
                "total_price": price * passengers,
                "stops": 0 if km < 1500 else rng.choice([0, 1]),
                "source": "mock",
            }
        )
    return sorted(options, key=lambda o: o["price_per_person"])


def search_trains(origin: str, destination: str, date: str, passengers: int) -> list[dict]:
    start, end = _resolve(origin, destination)
    if not (has_railway(start) and has_railway(end)):
        return []

    km = distance_km(start, end)
    rng = _rng("train", start, end, date)
    hours = round(km / 55 + 2, 1)

    options = []
    for name in rng.sample(TRAIN_NAMES, k=2):
        for travel_class, rate in (("SL", 0.55), ("3A", 1.4)):
            price = int((150 + km * rate) * rng.uniform(0.95, 1.1) / 10) * 10
            options.append(
                {
                    "id": str(rng.randint(12000, 12999)),
                    "mode": "train",
                    "provider": f"{name} ({travel_class})",
                    "origin": start,
                    "destination": end,
                    "date": date,
                    "depart_time": f"{rng.choice([5, 11, 16, 22]):02d}:30",
                    "duration_hours": hours if travel_class == "SL" else round(hours * 0.85, 1),
                    "price_per_person": price,
                    "total_price": price * passengers,
                    "travel_class": travel_class,
                    "source": "mock",
                }
            )
    return sorted(options, key=lambda o: o["price_per_person"])


def search_buses(origin: str, destination: str, date: str, passengers: int) -> list[dict]:
    start, end = _resolve(origin, destination)
    km = distance_km(start, end)
    if km > MAX_BUS_DISTANCE_KM:
        return []

    rng = _rng("bus", start, end, date)
    hours = round(km / 45 + 1, 1)

    options = []
    for operator in rng.sample(BUS_OPERATORS, k=2):
        price = int((100 + km * 0.95) * rng.uniform(0.9, 1.2) / 10) * 10
        options.append(
            {
                "id": f"BUS-{rng.randint(1000, 9999)}",
                "mode": "bus",
                "provider": operator,
                "origin": start,
                "destination": end,
                "date": date,
                "depart_time": f"{rng.choice([7, 14, 19, 21]):02d}:00",
                "duration_hours": hours,
                "price_per_person": price,
                "total_price": price * passengers,
                "bus_type": rng.choice(["AC Sleeper", "AC Seater", "Non-AC Sleeper"]),
                "source": "mock",
            }
        )
    return sorted(options, key=lambda o: o["price_per_person"])
