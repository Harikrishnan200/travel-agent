"""Real flight schedules, from AviationStack.

What the free plan gives, and what it does not, decides the whole design here:

- **It gives** the airlines, flight numbers and scheduled times that actually
  fly a route. Airlines run the same timetable most days, so today's Kochi to
  Delhi departures are a truthful picture of what flies that route.
- **It does not give** fares, and its `flight_date` parameter is restricted on
  the free plan, so we cannot ask about the traveller's actual date.

So a result here is a real flight on the traveller's date, at an estimated
price. Every record says so - `source` names the provider and
`price_estimated` is true - because a number we invented must never be
mistaken for one we were told.
"""

import logging

import httpx

from mcp_servers.cities import UnknownCityError, distance_km, iata_code, normalize_city
from mcp_servers.transport.mock_data import price_for_flight

logger = logging.getLogger(__name__)

API_URL = "https://api.aviationstack.com/v1/flights"

# Enough to compare on, without paging through a whole day's timetable.
MAX_FLIGHTS = 6


class FlightProviderError(RuntimeError):
    """AviationStack could not answer. We never fall back to mock data."""


def search_flights(
    origin: str,
    destination: str,
    date: str,
    passengers: int,
    api_key: str,
    timeout: float = 20.0,
) -> list[dict]:
    """Real flights on this route, priced by estimate. Raises rather than inventing."""
    start, end = normalize_city(origin), normalize_city(destination)
    if start is None:
        raise UnknownCityError(origin)
    if end is None:
        raise UnknownCityError(destination)

    departure, arrival = iata_code(start), iata_code(end)
    if not departure or not arrival:
        return []  # one of them has no airport

    try:
        response = httpx.get(
            API_URL,
            params={
                "access_key": api_key,
                "dep_iata": departure,
                "arr_iata": arrival,
                "limit": 100,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise FlightProviderError(f"AviationStack request failed: {exc}") from exc

    if "error" in payload:
        raise FlightProviderError(f"AviationStack: {payload['error'].get('message')}")

    return parse_flights(payload, start, end, date, passengers)


def parse_flights(
    payload: dict, origin: str, destination: str, date: str, passengers: int
) -> list[dict]:
    """Turn AviationStack's records into the shape the rest of the system uses."""
    km = distance_km(origin, destination)
    seen: dict[str, dict] = {}

    for record in payload.get("data", []):
        flight = _to_option(record, origin, destination, date, passengers, km)
        # One entry per flight number: a timetable repeats the same flight.
        if flight and flight["id"] not in seen:
            seen[flight["id"]] = flight

    flights = sorted(seen.values(), key=lambda f: f["price_per_person"])
    return flights[:MAX_FLIGHTS]


def _to_option(
    record: dict, origin: str, destination: str, date: str, passengers: int, km: float
) -> dict | None:
    flight_number = (record.get("flight") or {}).get("iata")
    airline = (record.get("airline") or {}).get("name")
    departure = (record.get("departure") or {}).get("scheduled")
    arrival = (record.get("arrival") or {}).get("scheduled")
    if not (flight_number and airline and departure and arrival):
        return None

    hours = _hours_between(departure, arrival)
    if hours is None or hours <= 0:
        return None

    # The price is ours, not theirs. Seeded by the flight number so the same
    # flight is quoted the same way twice.
    price = price_for_flight(km, seed=f"{flight_number}|{date}")

    return {
        "id": flight_number,
        "mode": "flight",
        "provider": airline,
        "origin": origin,
        "destination": destination,
        "date": date,
        "depart_time": departure[11:16],
        "duration_hours": hours,
        "price_per_person": price,
        "total_price": price * passengers,
        "stops": 0,
        "source": "aviationstack",
        # The schedule is real; the fare is not. Never let these be confused.
        "price_estimated": True,
    }


def _hours_between(departure: str, arrival: str) -> float | None:
    """Journey length from two ISO timestamps, tolerating their timezone suffixes."""
    from datetime import datetime

    try:
        start = datetime.fromisoformat(departure)
        end = datetime.fromisoformat(arrival)
    except (TypeError, ValueError):
        return None
    return round((end - start).total_seconds() / 3600, 1)
